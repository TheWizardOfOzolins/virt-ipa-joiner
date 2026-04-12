"""
Observability Integration Tests
=================================
Verifies /healthz, /readyz, and /metrics against a running app instance,
then confirms that a live Prometheus server has successfully scraped the app.

Required environment variables:
  APP_BASE_URL     — base URL of a running app (e.g. http://localhost:8080)

Optional environment variables:
  PROMETHEUS_URL   — base URL of a Prometheus instance (e.g. http://localhost:9090)
                     Prometheus scrape tests are skipped when this is unset.

The GitHub Actions workflow sets both variables and runs a real Prometheus
container to provide end-to-end coverage.  Locally, set APP_BASE_URL to an
already-running `uvicorn app.main:app` process; PROMETHEUS_URL is optional.
"""

import os
import time

import pytest
import requests

APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "").rstrip("/")

pytestmark = pytest.mark.skipif(
    not APP_BASE_URL,
    reason="APP_BASE_URL not set — skipping observability integration tests",
)


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


class TestHealthz:
    def test_returns_200(self):
        r = requests.get(f"{APP_BASE_URL}/healthz", timeout=5)
        assert r.status_code == 200

    def test_body_is_ok(self):
        r = requests.get(f"{APP_BASE_URL}/healthz", timeout=5)
        assert r.json() == {"status": "ok"}

    def test_content_type_is_json(self):
        r = requests.get(f"{APP_BASE_URL}/healthz", timeout=5)
        assert "application/json" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# /readyz
# ---------------------------------------------------------------------------


class TestReadyz:
    def test_returns_200(self):
        r = requests.get(f"{APP_BASE_URL}/readyz", timeout=5)
        assert r.status_code == 200

    def test_body_is_ready(self):
        r = requests.get(f"{APP_BASE_URL}/readyz", timeout=5)
        assert r.json() == {"status": "ready"}

    def test_content_type_is_json(self):
        r = requests.get(f"{APP_BASE_URL}/readyz", timeout=5)
        assert "application/json" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_returns_200(self):
        r = requests.get(f"{APP_BASE_URL}/metrics", timeout=5)
        assert r.status_code == 200

    def test_content_type_is_prometheus_plaintext(self):
        r = requests.get(f"{APP_BASE_URL}/metrics", timeout=5)
        assert "text/plain" in r.headers["content-type"]

    def test_exposition_format_has_help_and_type_lines(self):
        """Prometheus text format requires # HELP and # TYPE comment blocks."""
        r = requests.get(f"{APP_BASE_URL}/metrics", timeout=5)
        assert "# HELP" in r.text
        assert "# TYPE" in r.text

    def test_http_request_duration_metric_present(self):
        """The FastAPI instrumentator must export the per-handler latency histogram."""
        # Generate traffic so the metric has data.
        for endpoint in ("/healthz", "/readyz"):
            requests.get(f"{APP_BASE_URL}{endpoint}", timeout=5)

        r = requests.get(f"{APP_BASE_URL}/metrics", timeout=5)
        assert "http_request_duration_seconds" in r.text

    def test_http_requests_total_metric_present(self):
        """The request counter must appear after at least one request."""
        requests.get(f"{APP_BASE_URL}/healthz", timeout=5)

        r = requests.get(f"{APP_BASE_URL}/metrics", timeout=5)
        assert "http_requests_total" in r.text

    def test_endpoint_labels_recorded(self):
        """Metrics must carry handler labels for /healthz and /readyz."""
        requests.get(f"{APP_BASE_URL}/healthz", timeout=5)
        requests.get(f"{APP_BASE_URL}/readyz", timeout=5)

        r = requests.get(f"{APP_BASE_URL}/metrics", timeout=5)
        assert 'handler="/healthz"' in r.text
        assert 'handler="/readyz"' in r.text

    def test_process_metrics_present(self):
        """The default python/process collectors must be active."""
        r = requests.get(f"{APP_BASE_URL}/metrics", timeout=5)
        assert "process_resident_memory_bytes" in r.text
        assert "python_info" in r.text


# ---------------------------------------------------------------------------
# Prometheus scrape verification
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not PROMETHEUS_URL,
    reason="PROMETHEUS_URL not set — skipping Prometheus scrape verification",
)
class TestPrometheusScrape:
    def test_prometheus_is_healthy(self):
        """Prometheus /-/healthy must return 200."""
        r = requests.get(f"{PROMETHEUS_URL}/-/healthy", timeout=5)
        assert r.status_code == 200

    def test_app_target_is_up(self):
        """The virt-ipa-joiner scrape target must show health=up in Prometheus."""
        r = requests.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=10)
        assert r.status_code == 200

        active = r.json()["data"]["activeTargets"]
        # Find our target by job name (set in prometheus.yml)
        target = next(
            (t for t in active if t.get("labels", {}).get("job") == "virt-ipa-joiner"),
            None,
        )
        assert target is not None, (
            f"No virt-ipa-joiner target found in Prometheus. Active targets: "
            f"{[t.get('labels', {}).get('job') for t in active]}"
        )
        assert target["health"] == "up", (
            f"virt-ipa-joiner target health is '{target['health']}', "
            f"lastError: {target.get('lastError', 'none')}"
        )

    def test_prometheus_stores_http_request_duration(self):
        """
        After at least one scrape interval, Prometheus must hold
        http_request_duration_seconds data from the app.
        """
        # Generate some traffic so the metric has samples.
        for _ in range(3):
            requests.get(f"{APP_BASE_URL}/healthz", timeout=5)
            requests.get(f"{APP_BASE_URL}/readyz", timeout=5)
            requests.get(f"{APP_BASE_URL}/metrics", timeout=5)

        # Allow time for Prometheus to complete at least one scrape cycle.
        # The CI prometheus.yml sets scrape_interval: 5s; we wait a bit longer.
        time.sleep(8)

        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={
                "query": 'http_request_duration_seconds_count{job="virt-ipa-joiner"}'
            },
            timeout=10,
        )
        assert r.status_code == 200
        result = r.json()
        assert result["status"] == "success", f"Prometheus query failed: {result}"
        assert len(result["data"]["result"]) > 0, (
            "No http_request_duration_seconds_count series found for job=virt-ipa-joiner. "
            "Prometheus may not have scraped the app yet, or the metric is missing."
        )

    def test_prometheus_stores_http_requests_total(self):
        """http_requests_total counter must exist in Prometheus after a scrape."""
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": 'http_requests_total{job="virt-ipa-joiner"}'},
            timeout=10,
        )
        assert r.status_code == 200
        result = r.json()
        assert result["status"] == "success"
        assert len(result["data"]["result"]) > 0, (
            "No http_requests_total series found for job=virt-ipa-joiner."
        )

    def test_prometheus_target_scrape_url_is_metrics_endpoint(self):
        """The scrape URL Prometheus uses must be the /metrics endpoint."""
        r = requests.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=10)
        active = r.json()["data"]["activeTargets"]
        target = next(
            (t for t in active if t.get("labels", {}).get("job") == "virt-ipa-joiner"),
            None,
        )
        assert target is not None
        assert target["scrapeUrl"].endswith("/metrics"), (
            f"Unexpected scrape URL: {target['scrapeUrl']}"
        )
