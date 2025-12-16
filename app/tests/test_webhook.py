import pytest
from fastapi.testclient import TestClient
from app.main import app
import base64
import json
import jsonpatch

client = TestClient(app)

# Sample Admission Review Request
SAMPLE_REVIEW = {
    "request": {
        "uid": "1234-5678",
        "namespace": "default",
        "object": {
            "metadata": {
                "name": "test-vm",
                "namespace": "default",
                "labels": {"ipa-enroll": "true"} # Trigger enrollment
            },
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [],
                        "domain": {"devices": {"disks": []}}
                    }
                },
                "preference": {"name": "rhel-9"}
            }
        }
    }
}

@pytest.mark.asyncio
async def test_mutate_vm_success(mocker):
    # 1. Mock the dependencies
    # Mock IPA success returning a fake OTP
    mocker.patch("app.routers.webhook.ipa_host_add", return_value="secret-otp-123")
    
    # Mock K8s checks (Always say yes to enrollment)
    mocker.patch("app.routers.webhook.check_should_enroll", return_value=True)
    
    # Mock Background Tasks (so we don't actually spawn threads)
    mocker.patch("fastapi.BackgroundTasks.add_task")

    # 2. Make the Request
    response = client.post("/mutate", json=SAMPLE_REVIEW)
    
    assert response.status_code == 200
    data = response.json()
    
    # 3. Verify the Response Structure
    assert data["response"]["allowed"] is True
    assert data["response"]["patchType"] == "JSONPatch"
    
    # 4. Decode and Verify the Patch
    patch_decoded = base64.b64decode(data["response"]["patch"]).decode()
    patch_obj = json.loads(patch_decoded)
    
    # Check if cloud-init volume was added
    volume_patch = next((op for op in patch_obj if op["path"] == "/spec/template/spec/volumes/-"), None)
    assert volume_patch is not None
    
    user_data = volume_patch["value"]["cloudInitNoCloud"]["userData"]
    
    # Verify our commands were injected
    assert "ipa-client-install" in user_data
    assert "secret-otp-123" in user_data # Ensure OTP was passed
    assert "dnf install" in user_data # Default RHEL command

@pytest.mark.asyncio
async def test_mutate_vm_os_detection(mocker):
    """
    Verifies that providing a preference (e.g., 'ubuntu') triggers the 
    correct install command from the OS_MAP.
    """
    # 1. Mock dependencies
    mocker.patch("app.routers.webhook.ipa_host_add", return_value="otp-ubuntu")
    mocker.patch("app.routers.webhook.check_should_enroll", return_value=True)
    mocker.patch("fastapi.BackgroundTasks.add_task")

    # 2. Create a request with an "Ubuntu" preference
    ubuntu_review = {
        "request": {
            "uid": "ubuntu-req-uid",
            "namespace": "default",
            "object": {
                "metadata": {
                    "name": "ubuntu-vm",
                    "namespace": "default",
                    "labels": {"ipa-enroll": "true"}
                },
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [],
                            "domain": {"devices": {"disks": []}}
                        }
                    },
                    # This name 'ubuntu' matches a key in your config.py OS_MAP
                    "preference": {"name": "ubuntu"} 
                }
            }
        }
    }

    # 3. Send Request
    response = client.post("/mutate", json=ubuntu_review)
    assert response.status_code == 200
    data = response.json()

    # 4. Decode Patch
    patch_decoded = base64.b64decode(data["response"]["patch"]).decode()
    patch_obj = json.loads(patch_decoded)

    # 5. Extract Cloud-Init UserData
    volume_patch = next((op for op in patch_obj if op["path"] == "/spec/template/spec/volumes/-"), None)
    assert volume_patch is not None
    user_data = volume_patch["value"]["cloudInitNoCloud"]["userData"]

    # 6. Verify Ubuntu Commands
    # Should see apt-get (Ubuntu)
    assert "apt-get install" in user_data
    assert "DEBIAN_FRONTEND=noninteractive" in user_data
    
    # Should NOT see dnf (RHEL default)
    assert "dnf install" not in user_data
    
    # Should still see the enroll command
    assert "ipa-client-install" in user_data