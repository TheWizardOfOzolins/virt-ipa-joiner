from python_freeipa import Client
from app.config import CONFIG, logger
from typing import List, Tuple
import datetime
import random
import dns.resolver


# --- Helper: DNS SRV Resolver ---
def ipa_resolve_srv(service: str, protocol: str, domain: str) -> List[str]:
    """
    Resolves SRV records and returns A LIST of all valid hostnames,
    sorted by Priority (asc) and then randomized by Weight/Shuffle.

    Args:
        service: The service name (e.g., "_ldap")
        protocol: The protocol (e.g., "_tcp")
        domain: The domain name (e.g., "example.com")

    Returns:
        List of target hostnames strings (without trailing dots).
        Returns empty list [] if no records found.
    """
    query_name = f"{service}.{protocol}.{domain}"
    results = []

    try:
        answers = dns.resolver.resolve(query_name, "SRV")

        if not answers:
            logger.info(f"No SRV records found for {query_name}")
            return []

        # Group records by priority
        records_by_priority = {}
        for rdata in answers:
            prio = rdata.priority
            if prio not in records_by_priority:
                records_by_priority[prio] = []

            # Store target (stripped of dot)
            target = rdata.target.to_text().rstrip(".")
            records_by_priority[prio].append(target)

        # Process priorities in order (lowest number = highest priority)
        sorted_priorities = sorted(records_by_priority.keys())

        for prio in sorted_priorities:
            candidates = records_by_priority[prio]

            # Shuffle candidates of the same priority for basic load balancing
            random.shuffle(candidates)

            # Add them to the master list
            results.extend(candidates)

    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        logger.info(f"No SRV records found for {query_name} or domain missing")
    except Exception as e:
        logger.info(f"An error occurred during SRV lookup: {e}")

    return results


# --- Helper: Get Authenticated Client (Retry Logic) ---
def get_ipa_client():
    """
    Creates an authenticated Client by trying DNS candidates first,
    then falling back to the static IPA_HOST config.
    """
    candidate_hosts = []

    # 1. Try DNS Discovery (Dynamic)
    # Note: Use CONFIG.get to safely access keys, assuming 'DOMAIN' is the key in your config
    domain = CONFIG.get("DOMAIN") or CONFIG.get("domain")
    if domain:
        dns_hosts = ipa_resolve_srv("_kerberos", "_tcp", domain)
        if dns_hosts:
            logger.info(f"Discovered IPA servers via DNS: {dns_hosts}")
            candidate_hosts.extend(dns_hosts)

    # 2. Add Static Config (Fallback)
    # This handles "ipa1.example.com" or "ipa1,ipa2"
    static_config = CONFIG.get("IPA_HOST")
    if static_config:
        static_hosts = [h.strip() for h in static_config.split(",") if h.strip()]
        for h in static_hosts:
            if h not in candidate_hosts:
                candidate_hosts.append(h)

    if not candidate_hosts:
        raise RuntimeError(
            "No IPA servers found! Check your DNS SRV records or IPA_HOST configuration."
        )

    # 3. Connection Retry Loop
    errors = []
    for host in candidate_hosts:
        try:
            logger.debug(f"Attempting connection to FreeIPA server: {host}")

            # Initialize Client
            c = Client(host=host, verify_ssl=CONFIG["IPA_VERIFY_SSL"])
            c.login(CONFIG["IPA_USER"], CONFIG["IPA_PASS"])

            logger.info(f"Successfully authenticated to {host}")
            return c

        except Exception as e:
            logger.warning(f"Failed to connect to {host}: {e}")
            errors.append(f"{host}: {str(e)}")
            continue  # Try the next server

    # If loop finishes without returning, we failed everywhere
    raise RuntimeError(f"All IPA connection attempts failed. Errors: {errors}")


# --- Helper: Robust Command Executor ---
def execute_ipa_command(client, command, *args, **kwargs):
    try:
        if hasattr(client, command):
            return getattr(client, command)(*args, **kwargs)
        return getattr(client, command)(*args, **kwargs)
    except AttributeError:
        return client._request(command, list(args), kwargs)


# --- Action: Add Host to IPA ---
def ipa_host_add(vm_name: str, namespace: str, vm_uuid: str) -> Tuple[str, str]:
    client_ipa = get_ipa_client()
    fqdn = build_fqdn(vm_name, namespace)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    desc_text = f"Created by virt-joiner at {timestamp} | K8s UID: {vm_uuid}"

    logger.info(f"Registering host: {fqdn} on server {client_ipa.host}")
    try:
        execute_ipa_command(
            client_ipa, "host_add", fqdn, force=True, description=desc_text
        )
        otp = vm_uuid
        execute_ipa_command(client_ipa, "host_mod", fqdn, userpassword=otp)

        # Return the OTP *AND* the server we actually talked to
        return otp, client_ipa.host

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
    # Handle case sensitivity of config keys if needed
    domain = CONFIG.get("DOMAIN") or CONFIG.get("domain")
    return f"{vm_name}.{namespace}.{domain}"
