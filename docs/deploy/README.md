# Deployment

This guide explains how to deploy the `virt-ipa-joiner` component using Kustomize on an OpenShift or Kubernetes cluster.

## Prerequisites

- Access to the target cluster with `oc` or `kubectl`
- Kustomize installed (or use `oc kustomize` on OpenShift)
- The base and overlay directories from this repository cloned locally

## Update Secrets

Edit the secrets file for your specific cluster to provide the required FreeIPA credentials and domain information.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: virt-ipa-joiner-config
  namespace: openshift-cnv
type: Opaque
stringData:
  IPA_HOST: "ipa.example.com"
  IPA_USER: "admin"
  IPA_PASS: "Secret123!"
  DOMAIN: "abc.example.com"
  REALM: "example.com"
```

## Apply the Kustomization

Navigate to your cluster-specific overlay and apply the resources.
For OpenShift clusters:

```bash
cd overlays/cluster1
oc apply -k .
```

This will create or update the necessary resources in the openshift-cnv namespace.

## Verification

And check that the associated pods are running:

```bash
oc get pods -n openshift-cnv
```

## Configuration options

You can configure the application via **Environment Variables** or a `config.yaml` file mounted at the application root.

| Variable | Description | Default |
| :--- | :--- | :--- |
| `IPA_HOST` | Fallback hostname(s) if DNS discovery fails (comma-separated for multiple) | `ipa.example.com` |
| `IPA_USER` | User with add/del permissions | `admin` |
| `IPA_PASS` | Password for the user | *Required* |
| `IPA_VERIFY_SSL`| Verifys IPA tls certs | `false` |
| `DOMAIN` | Domain name for the host (FQDN) | `abc.example.com` |
| `REALM` | IPA REALM that the host is joining to | `EXAMPLE.COM`
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `FINALIZER_NAME` | K8s Finalizer string | `ipa.enroll/cleanup` |
| `CONFIG_PATH` | Path to config.yaml | `config.yaml` (in app root dir)

### Example `config.yaml`

```yaml
# Connectivity to FreeIPA / Red Hat IDM
# Used only if DNS SRV lookup (_kerberos._tcp.example.com) fails.
# You can specify a single host or a list: "ipa1.example.com,ipa2.example.com"
ipa_host: "ipa.example.com"
ipa_user: "admin"
# It is recommended to use an Environment Variable (IPA_PASS) for the password
# instead of writing it here, but you can uncomment this for local testing.
# ipa_pass: "SecretPassword123!"

# Set to false by default but in a production environment its probably worth setting this to true.
ipa_verify_ssl: false

# The DNS domain your VMs will join
domain: "abc.example.com"

#  IPA REALM that the host is joining to
realm: "EXAMPLE.COM"

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
