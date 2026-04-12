"""
Unit tests for /healthz, /readyz, and /metrics endpoints.

These tests run entirely in-process using TestClient. The controller task and
IPA health flag are patched directly so tests cover every branch without
needing a real Kubernetes cluster or FreeIPA server.
"""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

import app.main as main_module
import app.health as health_module
from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _running_task():
    """Return a mock task that looks like a still-running asyncio.Task."""
    t = MagicMock()
    t.done.return_value = False
    return t


def _done_task():
    """Return a mock task that looks like a completed asyncio.Task."""
    t = MagicMock()
    t.done.return_value = True
    return t


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def test_healthz_ok(mocker):
    """Returns 200 when the controller task is still running."""
    mocker.patch.object(main_module, "_controller_task", new=_running_task())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_no_task():
    """Returns 503 when the controller task has not been created (lifespan not started)."""
    # TestClient used without `with` statement never runs lifespan, so
    # _controller_task is None at module level.
    assert main_module._controller_task is None
    response = client.get("/healthz")
    assert response.status_code == 503
    assert "controller" in response.json()["detail"]


def test_healthz_task_done(mocker):
    """Returns 503 when the controller task has exited unexpectedly."""
    mocker.patch.object(main_module, "_controller_task", new=_done_task())
    response = client.get("/healthz")
    assert response.status_code == 503
    assert "controller" in response.json()["detail"]


# ---------------------------------------------------------------------------
# /readyz
# ---------------------------------------------------------------------------


def test_readyz_ok(mocker):
    """Returns 200 when controller is running and IPA is reachable."""
    mocker.patch.object(main_module, "_controller_task", new=_running_task())
    mocker.patch.object(health_module, "_ipa_healthy", new=True)
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readyz_no_task():
    """Returns 503 when controller task is None."""
    assert main_module._controller_task is None
    response = client.get("/readyz")
    assert response.status_code == 503
    assert "controller" in response.json()["detail"]


def test_readyz_task_done(mocker):
    """Returns 503 when the controller task has exited."""
    mocker.patch.object(main_module, "_controller_task", new=_done_task())
    response = client.get("/readyz")
    assert response.status_code == 503
    assert "controller" in response.json()["detail"]


def test_readyz_ipa_unhealthy(mocker):
    """Returns 503 with an IPA-specific message when IPA is marked unhealthy."""
    mocker.patch.object(main_module, "_controller_task", new=_running_task())
    mocker.patch.object(health_module, "_ipa_healthy", new=False)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert "IPA" in response.json()["detail"]


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------


def test_metrics_returns_200():
    response = client.get("/metrics")
    assert response.status_code == 200


def test_metrics_content_type_is_plaintext():
    response = client.get("/metrics")
    assert "text/plain" in response.headers["content-type"]


def test_metrics_exposition_format():
    """Response must be valid Prometheus text exposition format."""
    response = client.get("/metrics")
    assert "# HELP" in response.text
    assert "# TYPE" in response.text


def test_metrics_contains_http_request_metrics():
    """After making requests, the HTTP duration counter must be present."""
    # Warm up so the instrumentator has data to export.
    client.get("/healthz")
    client.get("/readyz")

    response = client.get("/metrics")
    assert "http_requests_total" in response.text
    assert "http_request_duration_seconds" in response.text


def test_metrics_records_endpoint_labels():
    """Metrics for /healthz and /readyz must appear with the correct handler label."""
    client.get("/healthz")
    client.get("/readyz")

    response = client.get("/metrics")
    text = response.text
    assert 'handler="/healthz"' in text
    assert 'handler="/readyz"' in text
