import pytest
from unittest.mock import MagicMock
import sys


# 1. Define a fake Exception to replace the real ApiException
class FakeApiException(Exception):
    def __init__(self, status=None, reason=None):
        self.status = status
        self.reason = reason


# 2. Create the Mock Module
mock_k8s_module = MagicMock()
# Attach the real exception class to the mock module
mock_k8s_module.client.ApiException = FakeApiException

# 3. Patch the system modules BEFORE importing app code
sys.modules["kubernetes_asyncio"] = mock_k8s_module
sys.modules["python_freeipa"] = MagicMock()


@pytest.fixture(autouse=True)
def reset_k8s_config():
    """Reset the K8s config-loaded flag before every test so tests are order-independent."""
    import app.services.k8s as k8s_module
    k8s_module._k8s_config_loaded = False
    yield
    k8s_module._k8s_config_loaded = False


@pytest.fixture
def mock_ipa_client(mocker):
    """Mocks the internal IPA client wrapper. Returns the mock client object."""
    mock_client = MagicMock()
    mocker.patch(
        "app.services.ipa.get_ipa_client",
        return_value=(mock_client, "ipa.example.com"),
    )
    return mock_client


@pytest.fixture
def mock_k8s_client(mocker):
    """Mocks Kubernetes client calls."""
    # Mock the API client context manager
    mock_api_instance = MagicMock()
    # Support 'async with client.ApiClient() as api_client:'
    mock_api_instance.__aenter__.return_value = mock_api_instance
    mock_api_instance.__aexit__.return_value = None

    mocker.patch("kubernetes_asyncio.client.ApiClient", return_value=mock_api_instance)
    return mock_k8s_module.client  # Return the client module wrapper
