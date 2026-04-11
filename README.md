# virt-ipa-joiner

**virt-ipa-joiner** is a Kubernetes/OpenShift controller and Mutating Webhook designed to automatically enroll **KubeVirt VirtualMachines** into **FreeIPA** (or Red Hat IDM).

It simplifies VM lifecycle management by handling identity registration at boot and cleanup at deletion.

## 🏗 Architecture

The application consists of two main components running in a single container:

1. **Mutating Webhook (FastAPI):** Intercepts `VirtualMachine` creation requests. It pre-creates the host in FreeIPA, generates an OTP, and injects a `cloud-init` script into the VM to install the IPA client automatically on first boot.
2. **Lifecycle Controller (AsyncIO):** Watches for VM deletion events to remove the host from FreeIPA. It also polls newly created VMs to verify that the enrollment was successful (checking for Keytab existence).

## 🔄 Order of Events

### Phase 1: VM Creation & Enrollment

1. **Intercept:** A user applies a `VirtualMachine` manifest. The Kubernetes API pauses the request and sends it to `virt-ipa-joiner`.
2. **Registration:** `virt-ipa-joiner` connects to the FreeIPA server, creates a new host entry, and generates a One-Time Password (OTP).
3. **Injection:** The VM configuration is patched (mutated) to include a `cloud-init` script containing the OTP and the `ipa-client-install` command.
4. **Boot:** The VM is allowed to start. On first boot, `cloud-init` runs the install command, using the OTP to join the domain securely.
5. **Verification:** The background controller polls FreeIPA to check if the host has uploaded its Keytab (indicating success) and emits a `Normal` event to the Kubernetes object.

### Phase 2: VM Deletion

1.**Watch:** When a user deletes the VM, the `virt-ipa-joiner` controller detects the deletion timestamp.
2. **Cleanup:** The controller connects to FreeIPA and deletes the host entry to ensure the directory remains clean.
3. **Finalize:** The Kubernetes Finalizer is removed, allowing the VM object to be fully deleted from the cluster.

## ✨ Features

* **Automatic Enrollment:** Injects `ipa-client-install` commands via Cloud-Init.
* **Automatic Cleanup:** Removes hosts from IPA when the KubeVirt VM is deleted.
* **DNS Auto-Discovery:** Automatically locates FreeIPA servers using _kerberos._tcp SRV records, ensuring high availability and load balancing without manual configuration.
* **Dynamic OS Support:** Automatically detects OS (RHEL/CentOS/Fedora vs Ubuntu/Debian) based on `instancetype` or `preference` and adjusts install commands (`dnf` vs `apt-get`).
* **InstanceType Inheritance:** Supports inheriting enrollment labels from `VirtualMachineClusterInstanceType`.
* **Security:** Runs as a non-root user (UID 1001) on Red Hat UBI 9.
* **Observability:** Emits native Kubernetes Events (`Normal` and `Warning`) to the VM object for enrollment status.

## 🚀 Deployment

[View the deployment README](docs/deploy/README.md)

### Service Discovery

`virt-ipa-joiner` attempts to locate FreeIPA servers in the following order:

1. **DNS SRV Records:** It queries `_kerberos._tcp.<DOMAIN>` to find all available servers. If multiple records are found, it respects priority and randomizes weight for load balancing.
2. **Static Configuration:** If no SRV records are found, it falls back to the `IPA_HOST` variable. This can be a single host or a comma-separated list (e.g., `ipa1.lab.com,ipa2.lab.com`).

## 💻 Development

### Local Setup

1. Create a virtual environment:

    ```bash
    python3.12 -m venv venv
    source venv/bin/activate
    ```

2. Install dependencies:

    ```bash
      pip install -r requirements.txt
      pip install -r requirements-test.txt
      pip install -r requirements-dev.txt
    ```

3. Install the git hooks

  ```bash
    pre-commit install --install-hooks
  ```

### Running Locally

To test the controller logic locally against a real K8s cluster and IPA server:

1. Export variables:

    ```bash
    export IPA_HOST="ipa.example.com"
    export IPA_USER="admin"
    export IPA_PASS="Secret123!"
    export IPA_VERIFY_SSL='False'
    export DOMAIN="example.com"
    export KUBECONFIG=~/.kube/config
    ```

2. Run with Uvicorn (Hot Reload):

    ```bash
    uvicorn app.main:app --reload --port 8080
    ```

### Testing

The test suite has three tiers, each covering a different layer of the stack.

#### Tier 1 — Unit Tests

Fast, no external dependencies. Run on every PR.

```bash
# Run all unit tests
pytest -v

# Run a specific module
pytest -v app/tests/test_workers.py
```

#### Tier 2 — Integration: Cloud-Init OS Matrix

Verifies that the webhook injects the correct OS-specific `ipa-client-install` command into the cloud-init payload for each supported OS. IPA is mocked — no real server required.

Run on every PR via GitHub Actions as a parallel matrix (one job per OS). Each job posts a synthetic `AdmissionReview` to the webhook and asserts the correct package manager command appears in the cloud-init `runcmd`.

```bash
# Run all OS types locally in a single pytest run
PYTHONPATH=. pytest tests/integration/test_os_matrix.py -v

# Run a single OS type (mirrors the matrix job)
TEST_OS_PREFERENCE=ubuntu-22-04 \
TEST_EXPECTED_CMD="apt-get install -y freeipa-client" \
TEST_BLOCKED_CMD="dnf install" \
PYTHONPATH=. pytest tests/integration/test_os_matrix.py::test_os_matrix_from_env -v
```

Supported OS values for `TEST_OS_PREFERENCE`: `rhel-9`, `fedora-41`, `ubuntu-22-04`, `debian-12`.

#### Tier 3 — Integration: IPA Lifecycle

Verifies the full host registration and cleanup lifecycle against a **real FreeIPA server**. Run on every PR via GitHub Actions (the CI spins up a FreeIPA container — expect ~5 min for this job).

To run locally, point the variables at a real FreeIPA instance:

```bash
export IPA_HOST=ipa.example.com
export IPA_USER=admin
export IPA_PASS=yourpassword
export IPA_VERIFY_SSL=false   # set to true in production
export DOMAIN=example.com
export REALM=EXAMPLE.COM

PYTHONPATH=. pytest tests/integration/test_ipa_lifecycle.py -v
```

Tests covered: host add creates entry, OTP is set after enroll, host delete removes entry, both operations are idempotent, connectivity and ping.

#### What is not yet tested

End-to-end VM boot testing (webhook fires → cloud-init runs → OS joins IPA inside a real VM) requires KubeVirt and is planned for a future iteration.

## 📦 Container Build

The project uses **Red Hat UBI 9** as the base image.

```bash
# Build with Podman
podman build -t virt-ipa-joiner:latest -f Containerfile .
```

## 🤝 Contributing

We welcome contributions! Follow these steps to submit bug fixes or new features:

1. **Make your changes:** Create an Issue with your proposed changes. Than create a new branch for your feature or fix.
2. **Submit a Pull Request:** Push your branch and open a PR against `main`.
3. **CI Verification:** The CI pipeline will automatically run the test suite against your code.
4. **Merge:** Once approved, your code will be merged into `main` (this does **not** trigger a new release image).

## 🚀 Releasing

To publish a new version of the application to GHCR:

1. **Bump the Version:** Update the semantic version number in the `VERSION` file.
2. **Submit a Release PR:** Open a Pull Request with the version change.
3. **Deploy:** Upon merging the version bump to `main`, the deployment pipeline will trigger automatically and push the new container image to GHCR with the corresponding version tag.

## 📜 License

Apache License 2.0
