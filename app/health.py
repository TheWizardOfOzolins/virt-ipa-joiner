"""
Centralized health state.

Flags are updated by service calls (get_ipa_client); probe endpoints
read them without making any external API calls.
"""

_ipa_healthy: bool = True


def set_ipa_healthy(value: bool) -> None:
    global _ipa_healthy
    _ipa_healthy = value


def is_ipa_healthy() -> bool:
    return _ipa_healthy
