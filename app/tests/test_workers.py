import pytest
from unittest.mock import AsyncMock  # <--- Fixed: Removed MagicMock
from app.services.k8s import send_delayed_creation_event, check_should_enroll

# Import the Fake exception class from where we injected it
from kubernetes_asyncio import client


@pytest.mark.asyncio
async def test_retry_logic_success(mocker, mock_k8s_client):
    # 1. Mock Sleep
    mock_sleep = mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    # 2. Setup the CustomObjectsApi with AsyncMock
    # We use AsyncMock because the code does 'await api.get_...'
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
    # Check args (namespace, name, uid, ...)
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
    # Setup AsyncMock for the API
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

    # Make the async call return the dict
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
