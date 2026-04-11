"""
OS Cloud-Init Injection Matrix Tests
=====================================
Verifies that the webhook injects the correct OS-specific package install
command into the cloud-init payload for each supported OS type.

These tests mock IPA so they are fast and self-contained — no external
services required. Each test runs as a separate GitHub Actions job in the
matrix, giving per-OS pass/fail visibility in the Actions UI.

The OS under test is driven by environment variables set by the matrix:
  TEST_OS_PREFERENCE  — value of spec.preference.name on the VM
  TEST_EXPECTED_CMD   — substring that MUST appear in the cloud-init runcmd
  TEST_BLOCKED_CMD    — substring that MUST NOT appear (wrong OS guard)

When run locally (no env vars set), all OS types are tested via pytest
parametrize so you can verify the full matrix in one shot.
"""

import base64
import json
import os
import sys
from unittest.mock import MagicMock

import yaml

import pytest

# ---------------------------------------------------------------------------
# Patch heavy system-level libraries before any app code is imported.
# python-freeipa requires libldap which may not be present in the test env.
# kubernetes_asyncio is not needed for this test.
# ---------------------------------------------------------------------------
if "kubernetes_asyncio" not in sys.modules:
    sys.modules["kubernetes_asyncio"] = MagicMock()
    sys.modules["kubernetes_asyncio.client"] = MagicMock()
    sys.modules["kubernetes_asyncio.config"] = MagicMock()
    sys.modules["kubernetes_asyncio.watch"] = MagicMock()

if "python_freeipa" not in sys.modules:
    sys.modules["python_freeipa"] = MagicMock()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

http_client = TestClient(app)

# ---------------------------------------------------------------------------
# OS test cases — each entry becomes one matrix job in GitHub Actions.
# ---------------------------------------------------------------------------
ALL_OS_CASES = [
    pytest.param(
        "rhel-9",
        "dnf install -y ipa-client",
        "apt-get",
        id="rhel",
    ),
    pytest.param(
        "fedora-41",
        "dnf install -y ipa-client",
        "apt-get",
        id="fedora",
    ),
    pytest.param(
        "ubuntu-22-04",
        "apt-get install -y freeipa-client",
        "dnf install",
        id="ubuntu",
    ),
    pytest.param(
        "debian-12",
        "apt-get install -y freeipa-client",
        "dnf install",
        id="debian",
    ),
]


def _decode_patch(response_data: dict) -> list:
    raw = response_data["response"]["patch"]
    return json.loads(base64.b64decode(raw).decode())


def _get_user_data(patch_ops: list) -> str:
    for op in patch_ops:
        if "volumes" in op.get("path", ""):
            return op["value"]["cloudInitNoCloud"]["userData"]
    return ""


def _get_runcmd_joined(user_data: str) -> str:
    """
    Parse the cloud-init YAML and return all runcmd entries joined into one
    string. This avoids false negatives from YAML line-wrapping of long commands.
    """
    parsed = yaml.safe_load(user_data) or {}
    return " ".join(parsed.get("runcmd", []))


def _make_review(preference: str) -> dict:
    return {
        "request": {
            "uid": "integ-test-uid-1234",
            "namespace": "default",
            "object": {
                "metadata": {
                    "name": "test-vm",
                    "namespace": "default",
                    "labels": {"ipa-enroll": "true"},
                },
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [],
                            "domain": {"devices": {"disks": []}},
                        }
                    },
                    "preference": {"name": preference},
                },
            },
        }
    }


def _run_os_test(mocker, preference: str, expected_cmd: str, blocked_cmd: str):
    """Core assertion logic shared between parametrized and env-var-driven modes."""
    mocker.patch(
        "app.routers.webhook.ipa_host_add",
        return_value=("test-otp-1234", "ipa.integration.test"),
    )
    mocker.patch("app.routers.webhook.check_should_enroll", return_value=True)
    # Prevent background tasks from being scheduled (no real K8s available).
    mocker.patch("fastapi.BackgroundTasks.add_task")

    response = http_client.post("/mutate", json=_make_review(preference))
    assert response.status_code == 200

    data = response.json()
    assert data["response"]["allowed"] is True
    assert "patch" in data["response"], "Expected a patch in the response"

    patch_ops = _decode_patch(data)
    user_data = _get_user_data(patch_ops)

    assert user_data, "cloud-init userData was not injected"

    # Parse the YAML so long lines that were wrapped by PyYAML are rejoined.
    runcmd = _get_runcmd_joined(user_data)

    assert expected_cmd in runcmd, (
        f"Expected '{expected_cmd}' in runcmd for preference '{preference}'.\n"
        f"runcmd: {runcmd}"
    )
    assert blocked_cmd not in runcmd, (
        f"Found unexpected '{blocked_cmd}' in runcmd for preference '{preference}'.\n"
        f"runcmd: {runcmd}"
    )
    assert "ipa-client-install" in runcmd, "ipa-client-install command missing"
    assert "--server=ipa.integration.test" in runcmd, "pinned IPA server missing"


# ---------------------------------------------------------------------------
# Mode 1: GitHub Actions matrix — single test driven by env vars.
# Each matrix job sets TEST_OS_PREFERENCE / TEST_EXPECTED_CMD / TEST_BLOCKED_CMD.
# ---------------------------------------------------------------------------
_preference = os.environ.get("TEST_OS_PREFERENCE", "")
_expected = os.environ.get("TEST_EXPECTED_CMD", "")
_blocked = os.environ.get("TEST_BLOCKED_CMD", "")


@pytest.mark.skipif(
    not _preference,
    reason="TEST_OS_PREFERENCE not set — running in parametrized mode instead",
)
def test_os_matrix_from_env(mocker):
    """Runs a single OS test driven by GitHub Actions matrix env vars."""
    _run_os_test(mocker, _preference, _expected, _blocked)


# ---------------------------------------------------------------------------
# Mode 2: Local development — all OS types tested in one pytest run.
# Skipped when running in matrix mode (env var is set).
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    bool(_preference),
    reason="Running in matrix mode (TEST_OS_PREFERENCE is set)",
)
@pytest.mark.parametrize("preference,expected_cmd,blocked_cmd", ALL_OS_CASES)
def test_os_matrix_all(mocker, preference, expected_cmd, blocked_cmd):
    """Tests all OS types locally in a single pytest run."""
    _run_os_test(mocker, preference, expected_cmd, blocked_cmd)
