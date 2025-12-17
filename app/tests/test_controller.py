import pytest
import asyncio
import datetime
from unittest.mock import MagicMock, AsyncMock
from app.services.k8s import run_controller, poll_ipa_keytab

# --- Fixtures ---
@pytest.fixture
def mock_k8s_watch(mocker):
    mock_watch = MagicMock()
    mock_stream = AsyncMock()
    mock_watch.stream.return_value = mock_stream
    mock_stream.__aenter__.return_value = mock_stream
    return mock_watch

@pytest.fixture
def mock_ipa_actions(mocker):
    return mocker.patch("app.services.k8s.ipa_host_del")

@pytest.fixture
def mock_send_event(mocker):
    # Fix: Patch the high-level 'delayed' sender so we don't need full K8s mocks
    return mocker.patch("app.services.k8s.send_delayed_creation_event", new_callable=AsyncMock)

# --- Tests ---

@pytest.mark.asyncio
async def test_controller_deletion_flow(mocker, mock_k8s_client, mock_ipa_actions):
    """
    Verifies that when a VM with a DeletionTimestamp is seen,
    we call IPA delete and remove the finalizer.
    """
    # This specific function calls send_k8s_event directly, so we mock that here
    mock_direct_event = mocker.patch("app.services.k8s.send_k8s_event", new_callable=AsyncMock)

    # 1. Mock Sleep to be instant
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    # 2. Setup Mock Event
    vm_name = "deleted-vm"
    namespace = "default"
    uid = "del-123"

    event_object = {
        "type": "MODIFIED",
        "object": {
            "apiVersion": "kubevirt.io/v1",
            "metadata": {
                "name": vm_name,
                "namespace": namespace,
                "uid": uid,
                "deletionTimestamp": "2024-01-01T12:00:00Z",
                "finalizers": ["ipa.enroll/cleanup"]
            }
        }
    }

    # 3. Define the Stream
    class MockStream:
        def __init__(self):
            self.events = [event_object]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.events:
                return self.events.pop(0)
            # Raise CancelledError to exit the 'while True' loop cleanly
            raise asyncio.CancelledError()

    mock_watch = mocker.patch("kubernetes_asyncio.watch.Watch")
    mock_watch.return_value.stream.return_value.__aenter__.return_value = MockStream()

    # 4. Mock API
    mock_cust_api = AsyncMock()
    mock_k8s_client.CustomObjectsApi.return_value = mock_cust_api

    # 5. Run Controller
    try:
        await run_controller()
    except asyncio.CancelledError:
        pass

    # 6. Assertions
    mock_ipa_actions.assert_called_once_with(vm_name, namespace)

    mock_direct_event.assert_called_with(
        namespace, vm_name, uid, "IPADeleteSuccess",
        "Removed host from IPA", "Normal", api_version="kubevirt.io/v1"
    )

    mock_cust_api.patch_namespaced_custom_object.assert_called_once()
    call_args = mock_cust_api.patch_namespaced_custom_object.call_args
    assert call_args.kwargs['name'] == vm_name
    assert call_args.kwargs['body'] == [{"op": "replace", "path": "/metadata/finalizers", "value": []}]

@pytest.mark.asyncio
async def test_keytab_poll_success(mocker, mock_send_event):
    # 1. Mock IPA
    mock_client = MagicMock()
    mocker.patch("app.services.k8s.get_ipa_client", return_value=mock_client)
    mock_exec = mocker.patch("app.services.k8s.execute_ipa_command")

    # 2. Mock Logic: Fail once, then succeed
    mock_exec.side_effect = [
        {"result": {"has_keytab": False}},
        {"result": {"has_keytab": True}}
    ]
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    # 3. Run
    await poll_ipa_keytab("default", "vm-success", "vm.example.com", timeout_minutes=1)

    # 4. Verify
    assert mock_exec.call_count == 2

    # Fix: We verify against the delayed sender signature
    mock_send_event.assert_called_with(
        "default",
        "vm-success",
        "IPAEnrollmentComplete",
        "Host Keytab found in IPA - Client installation successful",
        "Normal"
    )

@pytest.mark.asyncio
async def test_keytab_poll_timeout(mocker, mock_send_event):
    mock_client = MagicMock()
    mocker.patch("app.services.k8s.get_ipa_client", return_value=mock_client)
    mocker.patch("app.services.k8s.execute_ipa_command", return_value={"result": {"has_keytab": False}})
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    # Patch datetime with REAL datetime objects
    mock_dt = mocker.patch("app.services.k8s.datetime")

    # Define a base time
    start_time = datetime.datetime(2024, 1, 1, 12, 0, 0)

    # Side Effect:
    # 1. First call is for 'end_time' calculation (start + timeout)
    # 2. Second call is for the while loop check (start < end) -> True, enter loop
    # 3. Third call is for the next loop check (later > end) -> False, exit loop
    mock_dt.datetime.now.side_effect = [
        start_time,                                      # For calculating end_time
        start_time,                                      # Loop 1 check
        start_time + datetime.timedelta(minutes=100)     # Loop 2 check (timeout exceeded)
    ]

    # We must allow timedelta to work normally or mock it to match our logic
    # Since we are passing real datetime objects above, we can just let timedelta be the real one
    # But since we mocked 'app.services.k8s.datetime', we need to restore timedelta
    mock_dt.timedelta = datetime.timedelta

    await poll_ipa_keytab("default", "vm-fail", "vm.fail.com", timeout_minutes=1)

    mock_send_event.assert_called_with(
        "default",
        "vm-fail",
        "IPAEnrollmentTimeout",
        "Timed out waiting for Keytab. VM may have failed to boot or enroll.",
        "Warning"
    )
