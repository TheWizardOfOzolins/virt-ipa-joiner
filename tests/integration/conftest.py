"""
Integration test configuration.

Unlike the unit test conftest (app/tests/conftest.py), this file does NOT
patch kubernetes_asyncio or python_freeipa globally — integration tests
either use real connections or mock only what they specifically need.
"""

import pytest


@pytest.fixture
def admission_review():
    """Base AdmissionReview factory. Override 'preference' per test."""

    def _make(vm_name="test-vm", namespace="default", preference="rhel-9", labels=None):
        if labels is None:
            labels = {"ipa-enroll": "true"}
        return {
            "request": {
                "uid": "integ-test-uid-1234",
                "namespace": namespace,
                "object": {
                    "metadata": {
                        "name": vm_name,
                        "namespace": namespace,
                        "labels": labels,
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

    return _make
