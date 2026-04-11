"""
IPA Lifecycle Integration Tests
=================================
Tests the full create → verify → delete lifecycle against a real FreeIPA server.

Requires environment variables:
  IPA_HOST        — FreeIPA hostname (e.g. ipa.integration.test)
  IPA_USER        — admin username
  IPA_PASS        — admin password
  IPA_VERIFY_SSL  — set to "false" for self-signed certs (CI default)
  DOMAIN          — domain for FQDN construction (e.g. integration.test)
  REALM           — Kerberos realm (e.g. INTEGRATION.TEST)

These are set automatically by the GitHub Actions workflow.
To run locally, export the variables pointing at a real FreeIPA instance.
"""

import os
import uuid

import pytest

# Skip the entire module if IPA_HOST is not configured, so a plain
# 'pytest' run (unit tests only) does not fail looking for a server.
IPA_HOST = os.environ.get("IPA_HOST", "")

pytestmark = pytest.mark.skipif(
    not IPA_HOST,
    reason="IPA_HOST not set — skipping IPA lifecycle tests (requires real FreeIPA)",
)

from app.services.ipa import (  # noqa: E402
    build_fqdn,
    execute_ipa_command,
    get_ipa_client,
    ipa_host_add,
    ipa_host_del,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _host_exists(ipa_client, fqdn: str) -> bool:
    """Return True if the host entry exists in IPA."""
    try:
        result = execute_ipa_command(ipa_client, "host_show", fqdn)
        return result is not None
    except Exception as e:
        if "not found" in str(e).lower():
            return False
        raise


def _unique_vm(prefix="integ-test") -> tuple[str, str, str]:
    """Generate a unique VM name, namespace, and uuid for test isolation."""
    short_id = str(uuid.uuid4())[:8]
    return f"{prefix}-{short_id}", "integration", str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIPAHostLifecycle:
    """Full create → verify → delete lifecycle."""

    def test_host_add_creates_entry(self):
        """ipa_host_add should create a host in FreeIPA and return a valid OTP."""
        vm_name, namespace, vm_uuid = _unique_vm()
        fqdn = build_fqdn(vm_name, namespace)

        otp, server = ipa_host_add(vm_name, namespace, vm_uuid)

        assert otp == vm_uuid, "OTP should equal the VM UUID"
        assert server, "Should return the IPA server hostname used"

        # Verify host exists in IPA
        client, _ = get_ipa_client()
        assert _host_exists(client, fqdn), f"Host {fqdn} should exist in IPA after add"

        # Cleanup
        ipa_host_del(vm_name, namespace)

    def test_host_add_sets_otp(self):
        """The host entry should have a one-time-password set after add."""
        vm_name, namespace, vm_uuid = _unique_vm()
        fqdn = build_fqdn(vm_name, namespace)

        ipa_host_add(vm_name, namespace, vm_uuid)

        client, _ = get_ipa_client()
        result = execute_ipa_command(client, "host_show", fqdn)

        # The host should have a password set (has_password flag)
        host_data = result.get("result", result) if isinstance(result, dict) else {}
        assert host_data.get("has_password") is True, (
            "Host should have a password (OTP) set after enrollment"
        )

        # Cleanup
        ipa_host_del(vm_name, namespace)

    def test_host_del_removes_entry(self):
        """ipa_host_del should remove the host from FreeIPA."""
        vm_name, namespace, vm_uuid = _unique_vm()
        fqdn = build_fqdn(vm_name, namespace)

        ipa_host_add(vm_name, namespace, vm_uuid)
        ipa_host_del(vm_name, namespace)

        client, _ = get_ipa_client()
        assert not _host_exists(client, fqdn), (
            f"Host {fqdn} should no longer exist in IPA after delete"
        )

    def test_host_del_is_idempotent(self):
        """Deleting a host that does not exist should not raise an error."""
        vm_name, namespace, vm_uuid = _unique_vm()

        # Never added — delete should be a no-op
        ipa_host_del(vm_name, namespace)

    def test_host_add_is_idempotent(self):
        """Adding a host that already exists should succeed (overwrites OTP)."""
        vm_name, namespace, vm_uuid = _unique_vm()

        ipa_host_add(vm_name, namespace, vm_uuid)

        # Second add with a new UUID — should overwrite OTP, not raise
        new_uuid = str(uuid.uuid4())
        otp, server = ipa_host_add(vm_name, namespace, new_uuid)
        assert otp == new_uuid

        # Cleanup
        ipa_host_del(vm_name, namespace)

    def test_fqdn_format(self):
        """FQDN should follow the vmname.namespace.domain pattern."""
        domain = os.environ.get("DOMAIN", "integration.test")
        fqdn = build_fqdn("my-vm", "my-ns")
        assert fqdn == f"my-vm.my-ns.{domain}"


class TestIPAConnectivity:
    """Basic connectivity and auth checks."""

    def test_get_ipa_client_connects(self):
        """Should be able to authenticate to FreeIPA."""
        client, hostname = get_ipa_client()
        assert client is not None
        assert hostname, "Should return the hostname we connected to"

    def test_ipa_ping(self):
        """IPA server should respond to a ping."""
        client, _ = get_ipa_client()
        result = execute_ipa_command(client, "ping")
        assert result is not None, "IPA ping should return a result"
