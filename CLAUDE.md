# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`virt-ipa-joiner` is a Kubernetes/OpenShift mutating webhook + lifecycle controller that automatically enrolls KubeVirt `VirtualMachine` objects into FreeIPA (or Red Hat IDM). It runs as a single container with two concurrent components.

> **Note:** `main.py` at the repo root is an older prototype. The active application lives entirely under `app/`.

## Development Setup

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-test.txt -r requirements-dev.txt
pre-commit install --install-hooks
```

## Common Commands

```bash
# Run the app locally (requires env vars — see README)
uvicorn app.main:app --reload --port 8080

# Lint
ruff check .
ruff format --check .

# Run all tests
PYTHONPATH=. pytest -v

# Run a single test file
PYTHONPATH=. pytest -v app/tests/test_workers.py

# Build container
podman build -t virt-ipa-joiner:latest -f Containerfile .
```

Commits must follow the **Conventional Commits** spec (`feat:`, `fix:`, etc.) — enforced by a pre-commit hook.

## Architecture

### Two concurrent components in one process

`app/main.py` starts FastAPI and spawns `run_controller()` as a background asyncio task via the `lifespan` context manager.

**1. Mutating Webhook** (`app/routers/webhook.py` → `POST /mutate`)

- Receives Kubernetes `AdmissionReview` requests for `VirtualMachine` creation.
- Calls `check_should_enroll()` — a VM is enrolled if it has label `ipa-enroll: "true"`, or if its referenced `VirtualMachineClusterInstanceType` has that label (inheritance check requires a K8s API call).
- On enroll: calls `ipa_host_add()` to pre-create the host in FreeIPA using the admission UID as the OTP, then builds a JSON Patch that injects a `cloudinitdisk` volume with a `cloud-config` running `ipa-client-install`, adds a Kubernetes finalizer, and sets status annotations.
- Always returns `allowed: true` (soft-fail) so VM creation is never blocked even if IPA enrollment fails.
- Rejects VMs whose generated FQDN exceeds 64 characters (hard rejection).
- Background tasks fire after the webhook response: `send_delayed_creation_event` and `poll_ipa_keytab`.

**2. Lifecycle Controller** (`app/services/k8s.py` → `run_controller()`)

- Watches all `kubevirt.io/v1 virtualmachines` cluster-wide using `kubernetes_asyncio` watch streams with a 60-second timeout, restarting on any error.
- On deletion: detects `deletionTimestamp` + presence of the finalizer, calls `ipa_host_del()`, emits a K8s event, then patches out the finalizer to unblock K8s deletion.
- Idempotency: checks for an existing `IPADeleteSuccess` event before acting (prevents double-deletion on reconnect).

### Key service modules

| File | Responsibility |
|------|---------------|
| `app/config.py` | Layered config: defaults → `config.yaml` (lowercase keys) → env vars (uppercase). Exports `CONFIG` dict and `logger`. |
| `app/services/ipa.py` | FreeIPA client: DNS SRV discovery (`_kerberos._tcp.<DOMAIN>`), connection retry loop across multiple servers, `ipa_host_add` / `ipa_host_del`, FQDN builder. |
| `app/services/k8s.py` | Controller loop, keytab poller (`poll_ipa_keytab`, 15-minute timeout, 10-second intervals), K8s event emitter, finalizer removal, `check_should_enroll`. |

### Configuration precedence

`config.yaml` (lowercase keys like `ipa_host`) → overridden by env vars (`IPA_HOST`, `IPA_PASS`, `DOMAIN`, `REALM`, `LOG_LEVEL`, `FINALIZER_NAME`, `IPA_VERIFY_SSL`). `OS_MAP` in config.yaml is merged (not replaced) with defaults.

### OS detection for cloud-init

The `OS_MAP` config key maps OS name fragments (e.g. `ubuntu`, `debian`, `rhel`) to the package install command. The webhook inspects `spec.preference.name` of the VM and matches against these keys to pick the right install command. Default is `dnf install -y ipa-client`.

### FQDN format

`{vm_name}.{namespace}.{DOMAIN}` — must be ≤ 64 characters.

## Testing

Tests live in `app/tests/`. `conftest.py` patches `kubernetes_asyncio` and `python_freeipa` into `sys.modules` before any app code is imported — this is required because those libraries are not available in the test environment without system dependencies (openldap, cyrus-sasl). Always set `PYTHONPATH=.` when running pytest locally.

## Deployment

Deploy via Kustomize overlays under `docs/deploy/kustomize/`. The overlay at `overlays/cluster1/` contains the cluster-specific `Secret` with IPA credentials. The app runs in the `openshift-cnv` namespace as UID 1001 on Red Hat UBI 9.

## Releasing

Bump the version in the `VERSION` file, open a PR, and merge to `main`. The CI pipeline detects the new version tag and pushes the image to GHCR with `latest` and the version tag. Do not push the tag manually.
