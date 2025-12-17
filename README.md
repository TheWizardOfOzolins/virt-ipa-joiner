# virt-joiner

**virt-joiner** is a Kubernetes/OpenShift controller and Mutating Webhook designed to automatically enroll **KubeVirt VirtualMachines** into **FreeIPA** (or Red Hat IDM).

It simplifies VM lifecycle management by handling identity registration at boot and cleanup at deletion.

## 🏗 Architecture

The application consists of two main components running in a single container:

1. **Mutating Webhook (FastAPI):** Intercepts `VirtualMachine` creation requests. It pre-creates the host in FreeIPA, generates an OTP, and injects a `cloud-init` script into the VM to install the IPA client automatically on first boot.
2. **Lifecycle Controller (AsyncIO):** Watches for VM deletion events to remove the host from FreeIPA. It also polls newly created VMs to verify that the enrollment was successful (checking for Keytab existence).

## 🔄 Order of Events

### Phase 1: VM Creation & Enrollment
1.  **Intercept:** A user applies a `VirtualMachine` manifest. The Kubernetes API pauses the request and sends it to `virt-joiner`.
2.  **Registration:** `virt-joiner` connects to the FreeIPA server, creates a new host entry, and generates a One-Time Password (OTP).
3.  **Injection:** The VM configuration is patched (mutated) to include a `cloud-init` script containing the OTP and the `ipa-client-install` command.
4.  **Boot:** The VM is allowed to start. On first boot, `cloud-init` runs the install command, using the OTP to join the domain securely.
5.  **Verification:** The background controller polls FreeIPA to check if the host has uploaded its Keytab (indicating success) and emits a `Normal` event to the Kubernetes object.

### Phase 2: VM Deletion
1.  **Watch:** When a user deletes the VM, the `virt-joiner` controller detects the deletion timestamp.
2.  **Cleanup:** The controller connects to FreeIPA and deletes the host entry to ensure the directory remains clean.
3.  **Finalize:** The Kubernetes Finalizer is removed, allowing the VM object to be fully deleted from the cluster.

## ✨ Features

* **Automatic Enrollment:** Injects `ipa-client-install` commands via Cloud-Init.
* **Automatic Cleanup:** Removes hosts from IPA when the KubeVirt VM is deleted.
* **Dynamic OS Support:** Automatically detects OS (RHEL/CentOS/Fedora vs Ubuntu/Debian) based on `instancetype` or `preference` and adjusts install commands (`dnf` vs `apt-get`).
* **InstanceType Inheritance:** Supports inheriting enrollment labels from `VirtualMachineClusterInstanceType`.
* **Security:** Runs as a non-root user (UID 1001) on Red Hat UBI 9.
* **Observability:** Emits native Kubernetes Events (`Normal` and `Warning`) to the VM object for enrollment status.

## 🚀 Deployment

### Prerequisites

* OpenShift or Kubernetes cluster with KubeVirt installed.
* FreeIPA / Red Hat IDM server reachable from the cluster.
* A Service Account in IPA with permissions to add/delete hosts.

### 1. Create Secret

Create a secret containing your IPA credentials.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: virt-joiner-config
  namespace: virtualisation
type: Opaque
stringData:
  IPA_HOST: "ipa.example.com"
  IPA_USER: "admin"
  IPA_PASS: "Secret123!"
  DOMAIN: "example.com"
```

### 2. Infrastructure Manifests

This manifest sets up the Service Account, ClusterRoles (crucial for watching VMs and creating Events), Service, and Deployment.

TLS Note: The Service includes the service.beta.openshift.io/serving-cert-secret-name annotation. This is critical because the K8s API server only speaks HTTPS to webhooks. This annotation tells OpenShift to automatically generate a valid TLS certificate and put it in the virt-joiner-certs secret.

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: virt-joiner
  namespace: virtualisation
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: virt-joiner-role
rules:
  # Permissions to watch/patch VMs and check InstanceTypes
  - apiGroups: ["kubevirt.io"]
    resources: ["virtualmachines"]
    verbs: ["get", "list", "watch", "patch"]
  - apiGroups: ["instancetype.kubevirt.io"]
    resources: ["virtualmachineclusterinstancetypes", "virtualmachineinstancetypes"]
    verbs: ["get", "list"]
  # Permissions to create Events (for logging enrollment status)
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["create", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: virt-joiner-binding
subjects:
  - kind: ServiceAccount
    name: virt-joiner
    namespace: virtualisation
roleRef:
  kind: ClusterRole
  name: virt-joiner-role
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: v1
kind: Service
metadata:
  name: virt-joiner
  namespace: virtualisation
  annotations:
    # OpenShift magic: automatically generates the TLS secret
    service.beta.openshift.io/serving-cert-secret-name: virt-joiner-certs
spec:
  ports:
    - port: 443
      targetPort: 8443
  selector:
    app: virt-joiner
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: virt-joiner
  namespace: virtualisation
spec:
  replicas: 2
  selector:
    matchLabels:
      app: virt-joiner
  template:
    metadata:
      labels:
        app: virt-joiner
    spec:
      serviceAccountName: virt-joiner
      containers:
        - name: virt-joiner
          image: ghcr.io/YOUR_ORG/virt-joiner:latest
          imagePullPolicy: Always
          ports:
            - containerPort: 8443
          env:
            - name: IPA_HOST
              value: "ipa.example.com"
            - name: IPA_VERIFY_SSL
              value: "True"
            - name: IPA_USER
              value: "admin"
            - name: IPA_PASS
              valueFrom:
                secretKeyRef:
                  name: virt-joiner-config
                  key: IPA_PASS
            - name: DOMAIN
              value: "example.com"
          volumeMounts:
            - name: certs
              # Must match the path in your Containerfile CMD
              mountPath: /var/run/secrets/serving-cert
              readOnly: true
      volumes:
        - name: certs
          secret:
            secretName: virt-joiner-certs
```

### 3. Webhook Configuration

This registers your service as a Mutating Webhook.

CA Bundle Note: The annotation service.beta.openshift.io/inject-cabundle: "true" tells OpenShift to automatically inject the CA certificate that signed your service's cert. This ensures the API server trusts your webhook.

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: virt-joiner-webhook
  annotations:
    # OpenShift: Automatically injects the CA Bundle
    service.beta.openshift.io/inject-cabundle: "true"
webhooks:
  - name: virt-joiner.virtualisation.svc
    clientConfig:
      service:
        name: virt-joiner
        namespace: virtualisation
        path: "/mutate"
    rules:
      - operations: ["CREATE"]
        apiGroups: ["kubevirt.io"]
        apiVersions: ["v1"]
        resources: ["virtualmachines"]
    admissionReviewVersions: ["v1"]
    sideEffects: None
    timeoutSeconds: 10
    failurePolicy: Fail # Start with 'Fail' to ensure policies are enforced
```

## ⚙️ Configuration

You can configure the application via **Environment Variables** or a `config.yaml` file mounted at the application root.

| Variable | Description | Default |
| :--- | :--- | :--- |
| `IPA_HOST` | Hostname of FreeIPA server | `ipa.example.com` |
| `IPA_USER` | User with add/del permissions | `admin` |
| `IPA_PASS` | Password for the user | *Required* |
| `IPA_VERIFY_SSL`| Verifys IPA tls certs | `false` |
| `DOMAIN` | Domain name for the host (FQDN) | `example.com` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `FINALIZER_NAME` | K8s Finalizer string | `ipa.enroll/cleanup` |
| `CONFIG_PATH` | Path to config.yaml | `config.yaml` (in app root dir)

### Example `config.yaml`

```yaml
# Connectivity to FreeIPA / Red Hat IDM
ipa_host: "ipa.example.com"
ipa_user: "admin"
# It is recommended to use an Environment Variable (IPA_PASS) for the password
# instead of writing it here, but you can uncomment this for local testing.
# ipa_pass: "SecretPassword123!"

# Set to false by default but in a production environment its probably worth setting this to true.
ipa_verify_ssl: false

# The DNS domain your VMs will join
domain: "example.com"

# Logging verbosity: DEBUG, INFO, WARNING, ERROR
log_level: "INFO"

# The name of the Kubernetes Finalizer to attach to VMs
# This ensures the controller can block deletion until IPA cleanup is done.
finalizer_name: "ipa.enroll/cleanup"

# -----------------------------------------------------------------------------
# OS Mapping
# -----------------------------------------------------------------------------
# This map determines which install command to inject into cloud-init based on
# the VM's 'preference' or 'instancetype' name.
#
# Logic: If the VM preference contains the key (e.g. "ubuntu"),
# the corresponding command is used.
# -----------------------------------------------------------------------------
os_map:
  ubuntu: "export DEBIAN_FRONTEND=noninteractive && apt-get update -y && apt-get install -y freeipa-client"
  debian: "export DEBIAN_FRONTEND=noninteractive && apt-get update -y && apt-get install -y freeipa-client"
  rhel: "dnf install -y ipa-client"
```

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

We use `pytest` for unit and logic testing.

```bash
# Run all tests
pytest -v

# Run specific intense worker tests
pytest -v app/tests/test_workers.py
```

## 📦 Container Build

The project uses **Red Hat UBI 9** as the base image.

```bash
# Build with Podman
podman build -t virt-joiner:latest -f Containerfile .
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

## Licence

Apache License 2.0
