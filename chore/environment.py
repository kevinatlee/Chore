import re

from django.core.exceptions import ImproperlyConfigured


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


def parse_bool(value, *, name, default=False):
    if value is None or value == "":
        return default
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ImproperlyConfigured(
        f"{name} must be one of: 1, 0, true, false, yes, no, on, off."
    )


def parse_list(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def parse_app_fqdn(value):
    """Return a normalized hostname, rejecting URLs and ambiguous host values."""
    fqdn = (value or "").strip().rstrip(".").lower()
    if not fqdn:
        return ""
    if (
        "://" in fqdn
        or "/" in fqdn
        or ":" in fqdn
        or "@" in fqdn
        or not HOSTNAME_RE.fullmatch(fqdn)
    ):
        raise ImproperlyConfigured(
            "APP_FQDN must contain only a hostname, for example chore.example.com."
        )
    return fqdn


def fqdn_configuration(app_fqdn, allowed_hosts="", trusted_origins=""):
    fqdn = parse_app_fqdn(app_fqdn)
    hosts = parse_list(allowed_hosts)
    origins = parse_list(trusted_origins)
    if fqdn:
        hosts.insert(0, fqdn)
        origins.insert(0, f"https://{fqdn}")
    return {
        "fqdn": fqdn,
        "allowed_hosts": list(dict.fromkeys(hosts)),
        "trusted_origins": list(dict.fromkeys(origins)),
    }
