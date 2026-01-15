import asyncio
import datetime
from typing import Dict, Any, cast
from kubernetes_asyncio import client, config, watch

# Import shared config
from app.config import CONFIG, logger

# Import IPA actions needed for the polling/deletion logic
from app.services.ipa import ipa_host_del, get_ipa_client, execute_ipa_command


# --- HELPER: K8s Event Sender ---
async def send_k8s_event(
    namespace,
    name,
    uid,
    reason,
    message,
    event_type="Normal",
    api_version="kubevirt.io/v1",
):
    try:
        try:
            config.load_incluster_config()
        except Exception:
            await config.load_kube_config()

        async with client.ApiClient() as api_client:
            core_api = client.CoreV1Api(api_client)
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

            involved_object = {
                "apiVersion": api_version,
                "kind": "VirtualMachine",
                "name": name,
                "namespace": namespace,
            }
            if uid:
                involved_object["uid"] = uid

            event = {
                "metadata": {"generateName": f"{name}-ipa-", "namespace": namespace},
                "involvedObject": involved_object,
                "reason": reason,
                "message": message,
                "type": event_type,
                "source": {"component": "virt-ipa-joiner"},
                "firstTimestamp": timestamp,
                "lastTimestamp": timestamp,
                "count": 1,
            }

            # type: ignore prevents Pylance from flagging the coroutine as not awaitable
            await core_api.create_namespaced_event(namespace, event)  # type: ignore

    except Exception as e:
        logger.error(f"Failed to create K8s event: {e}")


# --- HELPER: Delayed Creation Event ---
async def send_delayed_creation_event(
    namespace, name, reason, message, event_type="Normal"
):
    """
    Polls K8s until the VM is found (persisted), then sends the event linked to its real UID.
    """
    logger.info(f"Background task: Waiting for creation of {name} to attach event...")
    for attempt in range(5):
        await asyncio.sleep(2)
        try:
            try:
                config.load_incluster_config()
            except Exception:
                await config.load_kube_config()

            async with client.ApiClient() as api_client:
                cust_api = client.CustomObjectsApi(api_client)
                try:
                    # type: ignore fixes Pylance "not awaitable" error
                    raw_vm = await cust_api.get_namespaced_custom_object(
                        group="kubevirt.io",
                        version="v1",
                        namespace=namespace,
                        plural="virtualmachines",
                        name=name,
                    )  # type: ignore

                    vm = cast(Dict[str, Any], raw_vm)

                except client.ApiException as e:
                    if e.status == 404:
                        logger.debug(
                            f"Attempt {attempt + 1}: VM {name} not found yet. Retrying..."
                        )
                        continue
                    raise e

                metadata = vm.get("metadata", {})
                if not isinstance(metadata, dict):
                    continue

                real_uid = metadata.get("uid")
                if not real_uid:
                    continue

                real_api = vm.get("apiVersion")
                if not isinstance(real_api, str):
                    real_api = "kubevirt.io/v1"

                logger.info(
                    f"Found VM {name} (UID: {real_uid}). Sending creation event."
                )
                await send_k8s_event(
                    namespace, name, real_uid, reason, message, event_type, real_api
                )
                return

        except Exception as e:
            logger.error(f"Error in delayed event loop for {name}: {e}")

    logger.warning(
        f"Gave up waiting for VM {name} to appear after 10 seconds. Event '{reason}' was dropped."
    )


# --- HELPER: Check Existing Event ---
async def event_already_exists(namespace, uid, reason):
    try:
        async with client.ApiClient() as api_client:
            core_api = client.CoreV1Api(api_client)
            events = await core_api.list_namespaced_event(
                namespace, field_selector=f"involvedObject.uid={uid}"
            )
            for e in events.items:
                if e.reason == reason:
                    return True
        return False
    except Exception as e:
        logger.warning(f"Failed to check existing events: {e}")
        return False


# --- HELPER: Remove Finalizer ---
async def remove_finalizer(api, namespace, name, current_finalizers):
    new_finalizers = [f for f in current_finalizers if f != CONFIG["FINALIZER_NAME"]]
    patch_body = [
        {"op": "replace", "path": "/metadata/finalizers", "value": new_finalizers}
    ]
    try:
        await api.patch_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=name,
            body=patch_body,
            _content_type="application/json-patch+json",
        )
    except client.ApiException as e:
        if e.status != 404:
            logger.error(f"Finalizer patch failed: {e}")


# --- HELPER: Poll IPA Keytab (Success Verification) ---
async def poll_ipa_keytab(namespace, name, fqdn, timeout_minutes=15):
    logger.info(f"Starting Keytab watcher for {fqdn} (Timeout: {timeout_minutes}m)")
    end_time = datetime.datetime.now() + datetime.timedelta(minutes=timeout_minutes)

    try:
        # UPDATED: Unpack the tuple (client, hostname)
        c, _ = get_ipa_client()
    except Exception as e:
        logger.error(f"Failed to create IPA client for polling: {e}")
        return

    while datetime.datetime.now() < end_time:
        try:
            result = execute_ipa_command(c, "host_show", fqdn)

            if not result:
                await asyncio.sleep(30)
                continue

            host_data = (
                result.get("result", result) if isinstance(result, dict) else result
            )

            if isinstance(host_data, dict):
                if host_data.get("has_keytab") is True:
                    logger.info(f"Keytab detected for {fqdn}! Enrollment complete.")
                    await send_delayed_creation_event(
                        namespace,
                        name,
                        "IPAEnrollmentComplete",
                        "Host Keytab found in IPA - Client installation successful",
                        "Normal",
                    )
                    return

        except Exception as e:
            # UPDATED: Log as warning so we see the root cause (e.g. "auth failed")
            logger.warning(f"Polling check failed for {fqdn}: {e}")
            try:
                logger.info("Attempting to switch/reconnect IPA client...")
                c, _ = get_ipa_client()
            except Exception as re_connect_error:
                logger.warning(
                    f"Failed to reconnect during polling: {re_connect_error}"
                )

        await asyncio.sleep(10)

    logger.warning(f"Keytab watcher timed out for {fqdn}")
    await send_delayed_creation_event(
        namespace,
        name,
        "IPAEnrollmentTimeout",
        "Timed out waiting for Keytab. VM may have failed to boot or enroll.",
        "Warning",
    )


# --- HELPER: Check Instance Type ---
async def check_should_enroll(vm_object, namespace):
    labels = vm_object["metadata"].get("labels", {})
    if labels.get("ipa-enroll") == "true":
        return True

    instancetype_ref = vm_object["spec"].get("instancetype")
    if not instancetype_ref:
        return False

    it_name = instancetype_ref.get("name")
    it_kind = instancetype_ref.get("kind", "VirtualMachineClusterInstanceType")

    if not it_name:
        return False

    logger.info(f"Checking InstanceType {it_name} ({it_kind}) for inheritance...")

    try:
        try:
            config.load_incluster_config()
        except Exception:
            await config.load_kube_config()

        async with client.ApiClient() as api_client:
            api = client.CustomObjectsApi(api_client)

            raw_obj = None
            if "cluster" in it_kind.lower():
                raw_obj = await api.get_cluster_custom_object(
                    group="instancetype.kubevirt.io",
                    version="v1beta1",
                    plural="virtualmachineclusterinstancetypes",
                    name=it_name,
                )  # type: ignore
            else:
                raw_obj = await api.get_namespaced_custom_object(
                    group="instancetype.kubevirt.io",
                    version="v1beta1",
                    plural="virtualmachineinstancetypes",
                    namespace=namespace,
                    name=it_name,
                )  # type: ignore

            it_obj = cast(Dict[str, Any], raw_obj)
            metadata = it_obj.get("metadata", {})

            if isinstance(metadata, dict):
                labels = metadata.get("labels", {})
                if isinstance(labels, dict) and labels.get("ipa-enroll") == "true":
                    logger.info(
                        f"Inherited ipa-enroll=true from InstanceType {it_name}"
                    )
                    return True

    except Exception as e:
        logger.warning(f"Failed to lookup InstanceType {it_name}: {e}")

    return False


# --- MAIN CONTROLLER LOOP ---
async def run_controller():
    logger.info("Starting Controller Watcher...")
    try:
        config.load_incluster_config()
    except Exception:
        await config.load_kube_config()

    while True:
        try:
            async with client.ApiClient() as api_client:
                api = client.CustomObjectsApi(api_client)

                async with watch.Watch().stream(
                    api.list_cluster_custom_object,
                    group="kubevirt.io",
                    version="v1",
                    plural="virtualmachines",
                    timeout_seconds=60,
                ) as stream:
                    async for event in stream:
                        if not isinstance(event, dict):
                            continue

                        obj = event.get("object")
                        if not isinstance(obj, dict):
                            continue

                        meta = obj.get("metadata", {})
                        name, uid, ns = (
                            meta.get("name"),
                            meta.get("uid"),
                            meta.get("namespace"),
                        )
                        finalizers = meta.get("finalizers", [])

                        obj_api_version = obj.get("apiVersion", "kubevirt.io/v1")

                        if CONFIG["FINALIZER_NAME"] not in finalizers:
                            continue
                        if not meta.get("deletionTimestamp"):
                            continue

                        # --- IDEMPOTENCY CHECK ---
                        if await event_already_exists(ns, uid, "IPADeleteSuccess"):
                            logger.debug(
                                f"Skipping {name}: Cleanup event already exists."
                            )
                            await remove_finalizer(api, ns, name, finalizers)
                            continue

                        logger.info(f"Processing deletion for {name}.{ns}...")
                        try:
                            # Calls IPA service to delete
                            ipa_host_del(name, ns)
                            await send_k8s_event(
                                ns,
                                name,
                                uid,
                                "IPADeleteSuccess",
                                "Removed host from IPA",
                                "Normal",
                                api_version=obj_api_version,
                            )
                            await remove_finalizer(api, ns, name, finalizers)
                        except Exception as e:
                            logger.error(f"Failed to delete {name}: {e}")
                            if not await event_already_exists(
                                ns, uid, "IPADeleteFailed"
                            ):
                                await send_k8s_event(
                                    ns,
                                    name,
                                    uid,
                                    "IPADeleteFailed",
                                    f"Failed: {e}",
                                    "Warning",
                                    api_version=obj_api_version,
                                )

        except Exception as e:
            logger.error(f"Watcher stream error: {e}. Restarting...")
            await asyncio.sleep(5)
