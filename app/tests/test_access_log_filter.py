import logging

from app.main import _AccessLogProbeFilter


def _make_record(client, method, path, status):
    """Build a uvicorn.access LogRecord with the exact args shape uvicorn uses."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(client, method, path, "1.1", status),
        exc_info=None,
    )


def test_filter_suppresses_successful_healthz():
    f = _AccessLogProbeFilter()
    assert f.filter(_make_record("10.0.0.1:1", "GET", "/healthz", 200)) is False


def test_filter_suppresses_successful_readyz():
    f = _AccessLogProbeFilter()
    assert f.filter(_make_record("10.0.0.1:1", "GET", "/readyz", 200)) is False


def test_filter_suppresses_successful_metrics():
    f = _AccessLogProbeFilter()
    assert f.filter(_make_record("10.0.0.1:1", "GET", "/metrics", 200)) is False


def test_filter_keeps_failed_probe():
    """Non-200 on a probe path must still be logged so failures stay visible."""
    f = _AccessLogProbeFilter()
    assert f.filter(_make_record("10.0.0.1:1", "GET", "/healthz", 503)) is True


def test_filter_keeps_mutate_endpoint():
    f = _AccessLogProbeFilter()
    assert f.filter(_make_record("10.0.0.1:1", "POST", "/mutate", 200)) is True


def test_filter_handles_query_string_on_probe_path():
    """If a probe URL ever picks up a query string, the path part still matches."""
    f = _AccessLogProbeFilter()
    assert f.filter(_make_record("10.0.0.1:1", "GET", "/healthz?probe=1", 200)) is False


def test_filter_passes_through_malformed_records():
    """A record with unexpected args shape must not crash the logger."""
    f = _AccessLogProbeFilter()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="something else",
        args=None,
        exc_info=None,
    )
    assert f.filter(record) is True
