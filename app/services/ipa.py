from python_freeipa import Client
from app.config import CONFIG, logger
import datetime


# --- Helper: IPA Client Wrapper ---
def get_ipa_client():
    c = Client(host=CONFIG["IPA_HOST"], verify_ssl=CONFIG["IPA_VERIFY_SSL"])
    c.login(CONFIG["IPA_USER"], CONFIG["IPA_PASS"])
    return c


# --- Helper: Robust Command Executor ---
def execute_ipa_command(client, command, *args, **kwargs):
    try:
        if hasattr(client, command):
            return getattr(client, command)(*args, **kwargs)
        return getattr(client, command)(*args, **kwargs)
    except AttributeError:
        return client._request(command, list(args), kwargs)


# --- Action: Add Host to IPA ---
def ipa_host_add(vm_name: str, namespace: str, vm_uuid: str) -> str:
    client_ipa = get_ipa_client()
    fqdn = build_fqdn(vm_name, namespace)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    desc_text = f"Created by virt-joiner at {timestamp} | K8s UID: {vm_uuid}"

    logger.info(f"Registering host: {fqdn}")
    try:
        execute_ipa_command(
            client_ipa, "host_add", fqdn, force=True, description=desc_text
        )
        otp = vm_uuid
        execute_ipa_command(client_ipa, "host_mod", fqdn, userpassword=otp)
        return otp
    except Exception as e:
        logger.error(f"IPA Add Error for {fqdn}: {e}")
        raise e


# --- Action: Delete Host from IPA ---
def ipa_host_del(vm_name: str, namespace: str):
    client_ipa = get_ipa_client()
    fqdn = build_fqdn(vm_name, namespace)
    logger.info(f"Deleting host: {fqdn}")
    try:
        execute_ipa_command(client_ipa, "host_del", fqdn)
    except Exception as e:
        if "not found" in str(e).lower():
            logger.info(f"Host {fqdn} already gone.")
            return
        logger.error(f"IPA Del Error for {fqdn}: {e}")
        raise e


# --- Helper: FQDN Construction ---
def build_fqdn(vm_name: str, namespace: str) -> str:
    return f"{vm_name}.{namespace}.{CONFIG['DOMAIN']}"
