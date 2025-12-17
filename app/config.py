import os
import yaml
import logging


def load_config(config_path=None):
    # If no path provided, check Env Var, then default to "config.yaml"
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", "config.yaml")

    # 1. Defaults
    conf = {
        "IPA_HOST": "ipa.example.com",
        "IPA_USER": "admin",
        "IPA_PASS": "password",
        "DOMAIN": "example.com",
        "IPA_VERIFY_SSL": False,
        "FINALIZER_NAME": "ipa.enroll/cleanup",
        "LOG_LEVEL": "INFO",
        "OS_MAP": {
            "ubuntu": "export DEBIAN_FRONTEND=noninteractive && apt-get update -y && apt-get install -y freeipa-client",
            "debian": "export DEBIAN_FRONTEND=noninteractive && apt-get update -y && apt-get install -y freeipa-client",
        },
    }

    # 2. Load from YAML
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                file_conf = yaml.safe_load(f) or {}
                for k in [
                    "ipa_host",
                    "ipa_user",
                    "ipa_pass",
                    "domain",
                    "ipa_verify_ssl",
                    "finalizer_name",
                    "log_level",
                ]:
                    if k in file_conf:
                        conf[k.upper()] = file_conf[k]

                if "os_map" in file_conf and isinstance(file_conf["os_map"], dict):
                    conf["OS_MAP"].update(file_conf["os_map"])
            print(f"Loaded configuration from {config_path}")
        except Exception as e:
            print(f"Warning: Failed to load config file at {config_path}: {e}")
    else:
        print(f"Info: No config file found at {config_path}, using defaults/env vars.")

    # 3. Load Env Vars (Overrides everything)
    conf["IPA_HOST"] = os.getenv("IPA_HOST", conf["IPA_HOST"])
    conf["IPA_USER"] = os.getenv("IPA_USER", conf["IPA_USER"])
    conf["IPA_PASS"] = os.getenv("IPA_PASS", conf["IPA_PASS"])
    conf["DOMAIN"] = os.getenv("DOMAIN", conf["DOMAIN"])
    conf["FINALIZER_NAME"] = os.getenv("FINALIZER_NAME", conf["FINALIZER_NAME"])
    conf["LOG_LEVEL"] = os.getenv("LOG_LEVEL", conf["LOG_LEVEL"]).upper()

    # We grab the value (which might be a bool from YAML or None from Env)
    # Then we cast it safely to ensure we end up with a proper Python boolean.
    ssl_val = os.getenv("IPA_VERIFY_SSL", conf["IPA_VERIFY_SSL"])
    conf["IPA_VERIFY_SSL"] = str(ssl_val).lower() == "true"

    return conf


CONFIG = load_config()

numeric_level = getattr(logging, CONFIG["LOG_LEVEL"], logging.INFO)
logging.basicConfig(
    level=numeric_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("virt-joiner")
