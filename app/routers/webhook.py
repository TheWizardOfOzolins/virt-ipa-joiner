import base64
import jsonpatch
import yaml
from typing import Dict, Any
from fastapi import APIRouter, Body, BackgroundTasks
from app.config import CONFIG, logger
from app.services.k8s import (
    check_should_enroll,
    send_delayed_creation_event,
    poll_ipa_keytab,
)
from app.services.ipa import ipa_host_add, build_fqdn

router = APIRouter()


@router.post("/mutate")
async def mutate_vm(
    background_tasks: BackgroundTasks, review: Dict[str, Any] = Body(...)
):
    request = review.get("request")
    if not request:
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"allowed": True},
        }

    admission_uid = request.get("uid")

    vm_object = request.get("object")
    if not vm_object:
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": admission_uid, "allowed": True},
        }

    vm_spec = vm_object.get("spec", {})
    object_meta = vm_object.get("metadata", {})

    if not vm_spec or not object_meta:
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": admission_uid, "allowed": True},
        }

    annotations = object_meta.get("annotations", {})
    vm_name = object_meta.get("name")

    namespace = request.get("namespace", object_meta.get("namespace", "default"))

    if not vm_name:
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": admission_uid, "allowed": True},
        }

    # Construct the target FQDN early to validate it
    fqdn = build_fqdn(vm_name, namespace)

    # Linux HOST_NAME_MAX is typically 64.
    if len(fqdn) > 64:
        error_msg = f"Generated FQDN '{fqdn}' is {len(fqdn)} chars. Max allowed is 64."
        logger.warning(f"Rejected VM {vm_name}: {error_msg}")
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {
                "uid": admission_uid,
                "allowed": False,
                "status": {"message": error_msg, "code": 400},
            },
        }
    # -----------------------------

    should_enroll = await check_should_enroll(vm_object, namespace)

    if not should_enroll:
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": admission_uid, "allowed": True},
        }

    patch = []
    otp = None
    enrollment_success = False
    status_msg = ""

    # 1. Attempt IPA Enrollment
    try:
        otp = ipa_host_add(vm_name, namespace, admission_uid)
        enrollment_success = True
        fqdn = build_fqdn(vm_name, namespace)
        status_msg = f"Enrolled as {fqdn}"

        # Send "Started" Event
        background_tasks.add_task(
            send_delayed_creation_event,
            namespace,
            vm_name,
            "IPAEnrollSuccess",
            f"Successfully pre-created host {fqdn} in IPA",
            "Normal",
        )

        # Start "Finished" Watcher (Keytab Polling)
        background_tasks.add_task(poll_ipa_keytab, namespace, vm_name, fqdn)

    except Exception as e:
        enrollment_success = False
        status_msg = f"Failed: {str(e)}"
        background_tasks.add_task(
            send_delayed_creation_event,
            namespace,
            vm_name,
            "IPAEnrollFailed",
            f"Failed to pre-create host in IPA: {str(e)}",
            "Warning",
        )

    if enrollment_success:
        fqdn = build_fqdn(vm_name, namespace)

        vm_template = vm_spec.get("template", {})
        template_spec = vm_template.get("spec", {})
        existing_volumes = template_spec.get("volumes", [])

        # --- DYNAMIC OS DETECTION ---
        install_cmd_str = "dnf install -y ipa-client"

        pref_name = ""
        vm_preference = vm_spec.get("preference", {})
        if vm_preference and "name" in vm_preference:
            pref_name = vm_preference["name"].lower()

        for os_key, os_cmd in CONFIG["OS_MAP"].items():
            if os_key in pref_name:
                logger.info(
                    f"Detected OS '{os_key}' from preference '{pref_name}'. Using custom install command."
                )
                install_cmd_str = os_cmd
                break

        ipa_cmd_parts = [
            "ipa-client-install",
            f"--server={CONFIG['IPA_HOST']}",
            f"--hostname={fqdn}",
            f"--domain={CONFIG['DOMAIN']}",
            f"--realm={CONFIG['DOMAIN'].upper()}",
            f"--password='{otp}'",
            "--mkhomedir",
            "--unattended",
            "--no-ntp",
        ]
        enroll_cmd_str = " ".join(ipa_cmd_parts)

        vol_index = -1
        for i, vol in enumerate(existing_volumes):
            if vol.get("name") == "cloudinitdisk":
                vol_index = i
                break

        if vol_index >= 0:
            cloud_init_no_cloud = existing_volumes[vol_index].get(
                "cloudInitNoCloud", {}
            )
            current_user_data_str = cloud_init_no_cloud.get("userData", "")
            try:
                cloud_config = yaml.safe_load(current_user_data_str) or {}
            except Exception:
                cloud_config = {}

            if "runcmd" not in cloud_config:
                cloud_config["runcmd"] = []

            cloud_config["runcmd"].append(install_cmd_str)
            cloud_config["runcmd"].append(enroll_cmd_str)

            cloud_config["hostname"] = vm_name
            cloud_config["fqdn"] = fqdn
            cloud_config["manage_etc_hosts"] = True

            new_user_data_str = "#cloud-config\n" + yaml.dump(cloud_config)

            patch.append(
                {
                    "op": "replace",
                    "path": f"/spec/template/spec/volumes/{vol_index}/cloudInitNoCloud/userData",
                    "value": new_user_data_str,
                }
            )
        else:
            cloud_config_data = {
                "hostname": vm_name,
                "fqdn": fqdn,
                "manage_etc_hosts": True,
                "runcmd": [install_cmd_str, enroll_cmd_str],
            }
            user_data = "#cloud-config\n" + yaml.dump(cloud_config_data)

            patch.append(
                {
                    "op": "add",
                    "path": "/spec/template/spec/volumes/-",
                    "value": {
                        "name": "cloudinitdisk",
                        "cloudInitNoCloud": {"userData": user_data},
                    },
                }
            )

            domain_spec = template_spec.get("domain", {})
            devices_spec = domain_spec.get("devices", {})
            existing_disks = devices_spec.get("disks", [])

            disk_names = [d.get("name") for d in existing_disks if d.get("name")]
            if "cloudinitdisk" not in disk_names:
                patch.append(
                    {
                        "op": "add",
                        "path": "/spec/template/spec/domain/devices/disks/-",
                        "value": {"name": "cloudinitdisk", "disk": {"bus": "virtio"}},
                    }
                )

        if "finalizers" in object_meta:
            patch.append(
                {
                    "op": "add",
                    "path": "/metadata/finalizers/-",
                    "value": CONFIG["FINALIZER_NAME"],
                }
            )
        else:
            patch.append(
                {
                    "op": "add",
                    "path": "/metadata/finalizers",
                    "value": [CONFIG["FINALIZER_NAME"]],
                }
            )

        if annotations:
            patch.append(
                {
                    "op": "add",
                    "path": "/metadata/annotations/ipa-enroll~1status",
                    "value": status_msg,
                }
            )
        else:
            patch.append(
                {
                    "op": "add",
                    "path": "/metadata/annotations",
                    "value": {"ipa-enroll/status": status_msg},
                }
            )
    else:
        if annotations:
            patch.append(
                {
                    "op": "add",
                    "path": "/metadata/annotations/ipa-enroll~1error",
                    "value": status_msg,
                }
            )
        else:
            patch.append(
                {
                    "op": "add",
                    "path": "/metadata/annotations",
                    "value": {"ipa-enroll/error": status_msg},
                }
            )

    patch_bytes = base64.b64encode(
        jsonpatch.JsonPatch(patch).to_string().encode()
    ).decode()
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {
            "uid": admission_uid,
            "allowed": True,
            "patchType": "JSONPatch",
            "patch": patch_bytes,
        },
    }
