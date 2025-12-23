from unittest.mock import MagicMock
import dns.resolver
from app.services.ipa import ipa_resolve_srv, get_ipa_client

# --- Test DNS Resolution ---


def test_ipa_resolve_srv_success(mocker):
    """Test that SRV records are parsed, sorted, and returned as a list."""

    # 1. Mock DNS Answer Object
    # We need to simulate the complex structure of dns.resolver.Answer
    mock_answer = MagicMock()

    # Create fake records (Priority, Weight, Port, Target)
    # Prio 10: host1, host2
    # Prio 20: host3
    record1 = MagicMock(
        priority=10, weight=100, port=88, target=MagicMock(to_text=lambda: "host1.")
    )
    record2 = MagicMock(
        priority=10, weight=100, port=88, target=MagicMock(to_text=lambda: "host2.")
    )
    record3 = MagicMock(
        priority=20, weight=100, port=88, target=MagicMock(to_text=lambda: "host3.")
    )

    mock_answer.__iter__.return_value = [record1, record2, record3]

    mocker.patch("dns.resolver.resolve", return_value=mock_answer)

    # 2. Run
    results = ipa_resolve_srv("_kerberos", "_tcp", "example.com")

    # 3. Assert
    assert len(results) == 3
    # host1 and host2 should be first (prio 10), host3 last (prio 20)
    assert "host3" == results[-1]
    assert "host1" in results[:2]
    assert "host2" in results[:2]


def test_ipa_resolve_srv_empty(mocker):
    """Test fallback when DNS returns nothing."""
    mocker.patch("dns.resolver.resolve", side_effect=dns.resolver.NoAnswer)
    results = ipa_resolve_srv("_kerberos", "_tcp", "example.com")
    assert results == []


# --- Test Client Retry Logic ---


def test_get_ipa_client_retry_flow(mocker):
    """
    Test that we try DNS hosts first, then Static hosts,
    and retry until login succeeds.
    """
    # 1. Mock Configuration
    mocker.patch.dict(
        "app.services.ipa.CONFIG",
        {
            "DOMAIN": "example.com",
            "IPA_HOST": "static-backup",
            "IPA_USER": "admin",
            "IPA_PASS": "pass",
            "IPA_VERIFY_SSL": False,
        },
    )

    # 2. Mock DNS Resolution
    mocker.patch("app.services.ipa.ipa_resolve_srv", return_value=["dns-host-1"])

    # 3. Mock Client
    MockClient = mocker.patch("app.services.ipa.Client")
    client_instance = MockClient.return_value

    # 4. Mock Login
    client_instance.login.side_effect = [
        Exception("Connection Refused"),  # dns-host-1 fails
        None,  # static-backup succeeds
    ]

    # 5. Run (UPDATED: Unpack the tuple)
    c, hostname = get_ipa_client()

    # 6. Assertions
    assert c == client_instance
    # Verify we got the hostname of the server that actually worked
    assert hostname == "static-backup"

    # Verify we tried initializing with both hosts in order
    from unittest.mock import call

    expected_calls = [
        call(host="dns-host-1", verify_ssl=False),
        call().login("admin", "pass"),
        call(host="static-backup", verify_ssl=False),
        call().login("admin", "pass"),
    ]
    MockClient.assert_has_calls(expected_calls, any_order=False)
