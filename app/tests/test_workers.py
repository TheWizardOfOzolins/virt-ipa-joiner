import pytest
from unittest.mock import AsyncMock, MagicMock
from kubernetes_asyncio import client
from app.services.k8s import (
    send_delayed_creation_event,
    check_should_enroll,
    poll_ipa_keytab,
)


@pytest.mark.asyncio
async def test_retry_logic_success(mocker, mock_k8s_client):
    # 1. Mock Sleep
    mock_sleep = mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    # 2. Setup the CustomObjectsApi with AsyncMock
    mock_cust_api = AsyncMock()
    mock_k8s_client.CustomObjectsApi.return_value = mock_cust_api

    # 3. Setup Side Effects (Fail twice, then succeed)
    mock_cust_api.get_namespaced_custom_object.side_effect = [
        client.ApiException(status=404),
        client.ApiException(status=404),
        {
            "metadata": {"uid": "real-uid-123", "name": "test-vm"},
            "apiVersion": "kubevirt.io/v1",
        },
    ]

    # 4. Mock Event Sender
    mock_send_event = mocker.patch(
        "app.services.k8s.send_k8s_event", new_callable=AsyncMock
    )

    # 5. Run
    await send_delayed_creation_event("default", "test-vm", "Reason", "Msg")

    # 6. Assert
    assert mock_sleep.call_count >= 2
    assert mock_cust_api.get_namespaced_custom_object.call_count == 3
    mock_send_event.assert_called_once()
    args = mock_send_event.call_args[0]
    assert args[2] == "real-uid-123"


@pytest.mark.asyncio
async def test_retry_logic_failure(mocker, mock_k8s_client):
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    mock_cust_api = AsyncMock()
    mock_k8s_client.CustomObjectsApi.return_value = mock_cust_api

    # Always raise 404
    mock_cust_api.get_namespaced_custom_object.side_effect = client.ApiException(
        status=404
    )

    mock_send_event = mocker.patch(
        "app.services.k8s.send_k8s_event", new_callable=AsyncMock
    )

    await send_delayed_creation_event("default", "ghost-vm", "Reason", "Msg")

    assert mock_cust_api.get_namespaced_custom_object.call_count == 5
    mock_send_event.assert_not_called()


@pytest.mark.asyncio
async def test_inheritance_logic(mocker, mock_k8s_client):
    mock_cust_api = AsyncMock()
    mock_k8s_client.CustomObjectsApi.return_value = mock_cust_api

    vm_object = {
        "metadata": {"labels": {}},
        "spec": {
            "instancetype": {
                "name": "large-type",
                "kind": "VirtualMachineClusterInstanceType",
            }
        },
    }

    mock_cust_api.get_cluster_custom_object.return_value = {
        "metadata": {"labels": {"ipa-enroll": "true"}}
    }

    should_enroll = await check_should_enroll(vm_object, "default")

    assert should_enroll is True
    mock_cust_api.get_cluster_custom_object.assert_called_with(
        group="instancetype.kubevirt.io",
        version="v1beta1",
        plural="virtualmachineclusterinstancetypes",
        name="large-type",
    )


@pytest.mark.asyncio
async def test_keytab_poll_reconnects_on_error(mocker):
    """
    Verifies that if polling encounters ANY error (e.g. network/timeout),
    it forces a reconnection by calling get_ipa_client() again.
    """
    # 1. Mock Sleep to run fast
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    # 2. Mock IPA Client & Command Execution
    # We track how many times get_ipa_client is called to verify reconnection
    mock_get_client = mocker.patch("app.services.k8s.get_ipa_client")
    mock_client_1 = MagicMock(name="client_1")
    mock_client_2 = MagicMock(name="client_2")

    mock_get_client.side_effect = [
        (mock_client_1, "ipa1.example.com"),
        (mock_client_2, "ipa2.example.com"),
    ]

    # 3. Mock the Delayed Event Sender (so we don't trigger the VM lookup loop)
    mock_delayed_event = mocker.patch(
        "app.services.k8s.send_delayed_creation_event", new_callable=AsyncMock
    )

    # 4. Mock Command Execution Sequence
    # Call 1 (using Client 1): Raises Exception (simulating network crash)
    # Call 2 (using Client 2): Returns Success (Found keytab)
    mock_exec = mocker.patch("app.services.k8s.execute_ipa_command")
    mock_exec.side_effect = [
        Exception("Connection Refused / Timeout"),
        {"result": {"has_keytab": True}},
    ]

    # 5. Run Polling
    await poll_ipa_keytab("default", "vm-retry", "vm.retry.com", timeout_minutes=1)

    # 6. Assertions
    # Crucial: Did we try to get a NEW client after the error?
    assert mock_get_client.call_count == 2

    # Did we verify the success event was eventually sent?
    mock_delayed_event.assert_called_with(
        "default",
        "vm-retry",
        "IPAEnrollmentComplete",
        "Host Keytab found in IPA - Client installation successful",
        "Normal",
    )
