"""URL and host checks shared by hosting, discovery and the command line."""

import ipaddress
from urllib.parse import urlsplit


def is_plain_http_url(url: str) -> bool:
    """Return whether ``url`` is an absolute http(s) URL with nothing secret in it.

    Credentials, a query or a fragment are refused because such URLs are
    published (on an Agent Card) or shared (in a registry source).

    Args:
        url: The URL to check.

    Returns:
        ``True`` for an absolute http or https URL with a host and without
        credentials, query or fragment; otherwise ``False``.
    """
    try:
        parts = urlsplit(url)
        # Reading the port validates it: a non-number or out-of-range port raises.
        parts.port  # noqa: B018
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and "@" not in parts.netloc
        and not parts.query
        and not parts.fragment
    )


def is_loopback_host(host: str) -> bool:
    """Return whether ``host`` names this machine: ``localhost`` or a loopback IP.

    Args:
        host: A host name or IP address, without brackets or port.

    Returns:
        ``True`` for ``localhost`` (any case) and loopback addresses such as
        ``127.0.0.1`` and ``::1``; otherwise ``False``.
    """
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
