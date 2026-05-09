"""
Webhook → IPA Integration Tests
=================================
Exercises the full /mutate HTTP path against a running app and a real FreeIPA
server, covering three gaps left by the unit and lifecycle test suites.

Gap 1 — Webhook → IPA registration
  POST /mutate → host pre-created in FreeIPA with OTP set, cloud-init patch
  injected, finalizer included.

Gap 2 — Soft-fail under IPA outage
  POST /mutate when IPA is completely unreachable → still returns allowed:True
  so VM creation is never blocked; error annotation written instead of
  cloud-init patch.

Gap 3 — Admission retry idempotency
  K8s retries the admission request for the same VM (same name, new UID) →
  both calls return allowed:True and IPA ends up with exactly one host entry.

Required environment variables:
  APP_BASE_URL       — running app with a valid IPA connection (gaps 1 & 3)
  SOFT_FAIL_APP_URL  — running app with an unreachable IPA_HOST (gap 2)

IPA verification (gaps 1 & 3) also requires:
  IPA_HOST, IPA_USER, IPA_PASS, IPA_VERIFY_SSL, DOMAIN, REALM

All variables are set automatically by the webhook-integration CI job.
To run locally point APP_BASE_URL at a running `uvicorn app.main:app` instance.
"""

import base64
import json
import os
import uuid

import pytest
import requests

APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
SOFT_FAIL_APP_URL = os.environ.get("SOFT_FAIL_APP_URL", "").rstrip("/")
IPA_HOST = os.environ.get("IPA_HOST", "")

# The entire module requires a running app.
pytestmark = pytest.mark.skipif(
    not APP_BASE_URL,
    reason="APP_BASE_URL not set — skipping webhook integration tests",
)

# IPA service helpers — only imported when IPA_HOST is set so that the
# soft-fail tests (gap 2) can run without system LDAP libraries.
if IPA_HOST:
    from app.services.ipa import (  # noqa: E402
        build_fqdn,
        execute_ipa_command,
        get_ipa_client,
        ipa_host_del,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NAMESPACE = "integration"


def _make_review(vm_name: str, uid: str | None = None) -> dict:
    """Build a minimal AdmissionReview CREATE request for a VM."""
    if uid is None:
        uid = str(uuid.uuid4())
    return {
        "request": {
            "uid": uid,
            "namespace": _NAMESPACE,
            "object": {
                "metadata": {
                    "name": vm_name,
                    "namespace": _NAMESPACE,
                    "labels": {"ipa-enroll": "true"},
                },
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [],
                            "domain": {"devices": {"disks": []}},
                        }
                    },
                    "preference": {"name": "rhel-9"},
                },
            },
        }
    }


def _decode_patch(response_data: dict) -> list:
    return json.loads(base64.b64decode(response_data["response"]["patch"]).decode())


def _unique_vm() -> str:
    """Generate a unique VM name safe for use as an IPA hostname."""
    return f"wh-{str(uuid.uuid4())[:8]}"


# ---------------------------------------------------------------------------
# Gap 1 — Webhook → IPA registration
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not IPA_HOST,
    reason="IPA_HOST not set — skipping IPA verification tests",
)
class TestWebhookIPARegistration:
    """POST /mutate registers the host in FreeIPA and returns a valid patch."""

    def test_host_created_in_ipa(self):
        """After a successful admission the host must exist in FreeIPA."""
        vm_name = _unique_vm()
        try:
            r = requests.post(
                f"{APP_BASE_URL}/mutate", json=_make_review(vm_name), timeout=30
            )
            assert r.status_code == 200
            assert r.json()["response"]["allowed"] is True

            client, _ = get_ipa_client()
            fqdn = build_fqdn(vm_name, _NAMESPACE)
            result = execute_ipa_command(client, "host_show", fqdn)
            assert result is not None, f"Host {fqdn} not found in IPA after /mutate"
        finally:
            ipa_host_del(vm_name, _NAMESPACE)

    def test_ipa_host_has_otp_set(self):
        """The pre-created host must have a one-time password (has_password=True)."""
        vm_name = _unique_vm()
        try:
            requests.post(
                f"{APP_BASE_URL}/mutate", json=_make_review(vm_name), timeout=30
            )
            client, _ = get_ipa_client()
            fqdn = build_fqdn(vm_name, _NAMESPACE)
            result = execute_ipa_command(client, "host_show", fqdn)
            host_data = result.get("result", result) if isinstance(result, dict) else {}
            assert host_data.get("has_password") is True, (
                "IPA host should have a password (OTP) set after webhook admission"
            )
        finally:
            ipa_host_del(vm_name, _NAMESPACE)

    def test_patch_contains_ipa_client_install(self):
        """The cloud-init runcmd must include ipa-client-install."""
        vm_name = _unique_vm()
        try:
            r = requests.post(
                f"{APP_BASE_URL}/mutate", json=_make_review(vm_name), timeout=30
            )
            patch = _decode_patch(r.json())
            volume_op = next(
                (op for op in patch if "volumes" in op.get("path", "")), None
            )
            assert volume_op is not None, (
                "No volume patch found — cloud-init not injected"
            )
            user_data = volume_op["value"]["cloudInitNoCloud"]["userData"]
            assert "ipa-client-install" in user_data
            assert build_fqdn(vm_name, _NAMESPACE) in user_data
        finally:
            ipa_host_del(vm_name, _NAMESPACE)

    def test_patch_contains_finalizer(self):
        """The patch must add the cleanup finalizer so the controller can act on deletion."""
        vm_name = _unique_vm()
        try:
            r = requests.post(
                f"{APP_BASE_URL}/mutate", json=_make_review(vm_name), timeout=30
            )
            patch = _decode_patch(r.json())
            finalizer_op = next(
                (op for op in patch if "finalizers" in op.get("path", "")), None
            )
            assert finalizer_op is not None, "No finalizer patch found"
        finally:
            ipa_host_del(vm_name, _NAMESPACE)


# ---------------------------------------------------------------------------
# Gap 2 — Soft-fail when IPA is unreachable
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not SOFT_FAIL_APP_URL,
    reason="SOFT_FAIL_APP_URL not set — skipping soft-fail tests",
)
class TestWebhookSoftFail:
    """VM creation must never be blocked even when IPA is completely unreachable."""

    def test_allowed_true_when_ipa_down(self):
        """Webhook must return allowed:True regardless of IPA connectivity."""
        r = requests.post(
            f"{SOFT_FAIL_APP_URL}/mutate",
            json=_make_review(_unique_vm()),
            timeout=30,
        )
        assert r.status_code == 200
        assert r.json()["response"]["allowed"] is True

    def test_no_cloud_init_injected_on_ipa_failure(self):
        """On IPA failure no cloud-init volume should be added to the patch."""
        r = requests.post(
            f"{SOFT_FAIL_APP_URL}/mutate",
            json=_make_review(_unique_vm()),
            timeout=30,
        )
        patch = _decode_patch(r.json())
        volume_ops = [op for op in patch if "volumes" in op.get("path", "")]
        assert len(volume_ops) == 0, (
            "cloud-init volume was injected despite IPA being unreachable"
        )

    def test_no_finalizer_added_on_ipa_failure(self):
        """No finalizer should be added when enrollment fails — nothing to clean up."""
        r = requests.post(
            f"{SOFT_FAIL_APP_URL}/mutate",
            json=_make_review(_unique_vm()),
            timeout=30,
        )
        patch = _decode_patch(r.json())
        finalizer_ops = [op for op in patch if "finalizers" in op.get("path", "")]
        assert len(finalizer_ops) == 0, (
            "Finalizer was added despite IPA enrollment failing"
        )

    def test_error_annotation_written_on_ipa_failure(self):
        """An ipa-enroll/error annotation must record the failure reason."""
        r = requests.post(
            f"{SOFT_FAIL_APP_URL}/mutate",
            json=_make_review(_unique_vm()),
            timeout=30,
        )
        patch = _decode_patch(r.json())
        annotation_ops = [op for op in patch if "annotations" in op.get("path", "")]
        assert len(annotation_ops) > 0, "No error annotation written on IPA failure"


# ---------------------------------------------------------------------------
# Gap 3 — Admission retry idempotency
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not IPA_HOST,
    reason="IPA_HOST not set — skipping idempotency tests",
)
class TestWebhookIdempotency:
    """K8s may re-send the same admission request; both calls must succeed."""

    def test_second_admission_for_same_vm_returns_allowed(self):
        """A second /mutate call for the same VM name must also return allowed:True."""
        vm_name = _unique_vm()
        try:
            r1 = requests.post(
                f"{APP_BASE_URL}/mutate", json=_make_review(vm_name), timeout=30
            )
            assert r1.json()["response"]["allowed"] is True

            # K8s retry: same VM name, fresh UID
            r2 = requests.post(
                f"{APP_BASE_URL}/mutate",
                json=_make_review(vm_name, uid=str(uuid.uuid4())),
                timeout=30,
            )
            assert r2.status_code == 200
            assert r2.json()["response"]["allowed"] is True
        finally:
            ipa_host_del(vm_name, _NAMESPACE)

    def test_two_admissions_produce_one_ipa_host(self):
        """Two admissions for the same VM name must not create duplicate IPA entries."""
        vm_name = _unique_vm()
        try:
            for _ in range(2):
                requests.post(
                    f"{APP_BASE_URL}/mutate",
                    json=_make_review(vm_name, uid=str(uuid.uuid4())),
                    timeout=30,
                )
            client, _ = get_ipa_client()
            fqdn = build_fqdn(vm_name, _NAMESPACE)
            # host_show raises if the host doesn't exist; no assertion needed
            result = execute_ipa_command(client, "host_show", fqdn)
            assert result is not None, f"Host {fqdn} missing after two admissions"
        finally:
            ipa_host_del(vm_name, _NAMESPACE)

    def test_second_admission_overwrites_otp(self):
        """The retry must refresh the OTP — the host should still have has_password=True."""
        vm_name = _unique_vm()
        try:
            for _ in range(2):
                requests.post(
                    f"{APP_BASE_URL}/mutate",
                    json=_make_review(vm_name, uid=str(uuid.uuid4())),
                    timeout=30,
                )
            client, _ = get_ipa_client()
            fqdn = build_fqdn(vm_name, _NAMESPACE)
            result = execute_ipa_command(client, "host_show", fqdn)
            host_data = result.get("result", result) if isinstance(result, dict) else {}
            assert host_data.get("has_password") is True
        finally:
            ipa_host_del(vm_name, _NAMESPACE)
