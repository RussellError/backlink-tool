"""URL validation, normalisation and SSRF protection.

Every URL that the crawler, checker or submitter touches goes through
validate_url(). Blocks localhost, private / link-local / reserved ranges,
cloud metadata endpoints, credentials in URLs and unusual ports.

Residual risk (documented in README): DNS is resolved at validation time and
again by the HTTP library when connecting, so a hostile DNS server could in
theory answer differently the second time (DNS rebinding).
"""
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = (80, 443, 8080, 8443)
MAX_URL_LENGTH = 2048

BLOCKED_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata.google.internal", "metadata", "instance-data",
    "kubernetes.default.svc",
}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa",
                    ".localdomain", ".intranet", ".corp")
METADATA_IPS = {"169.254.169.254", "169.254.170.2", "100.100.100.200",
                "fd00:ec2::254", "192.0.0.192"}
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$|ref_src$)", re.I)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class UnsafeURL(ValueError):
    """Raised when a URL is malformed or points somewhere we must not fetch."""


def is_ip_blocked(ip):
    """True if the address is not a normal public internet address."""
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip.strip("[]"))
        except ValueError:
            return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if str(ip) in METADATA_IPS:
        return True
    return (not ip.is_global) or ip.is_multicast or ip.is_loopback or ip.is_link_local \
        or ip.is_private or ip.is_reserved or ip.is_unspecified


def normalize_user_url(raw):
    """Add https:// to bare domains typed by the user."""
    raw = (raw or "").strip()
    if not raw:
        raise UnsafeURL("URL is empty")
    if "://" not in raw:
        raw = "https://" + raw
    return raw


def validate_url(url, resolve=True):
    """Return a cleaned URL or raise UnsafeURL. resolve=False skips DNS."""
    if not isinstance(url, str):
        raise UnsafeURL("URL must be text")
    url = url.strip()
    if not url:
        raise UnsafeURL("URL is empty")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURL("URL is too long")
    if any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise UnsafeURL("URL contains spaces or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeURL("URL is malformed")
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURL("Only http:// and https:// URLs are allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURL("URL has no host name")
    if parts.username or parts.password:
        raise UnsafeURL("URLs containing credentials are not allowed")
    if port is not None and port not in ALLOWED_PORTS:
        raise UnsafeURL(f"Port {port} is not allowed")
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURL("Local and internal host names are blocked")
    literal = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    if literal is not None:
        if is_ip_blocked(literal):
            raise UnsafeURL("Private, loopback and reserved IP addresses are blocked")
    else:
        if "." not in host:
            raise UnsafeURL("Host name must contain a dot (e.g. example.com)")
        if not re.match(r"^[a-z0-9._\-\u00a1-\uffff]+$", host):
            raise UnsafeURL("Host name contains invalid characters")
        if resolve:
            _check_resolution(host, port or (443 if parts.scheme.lower() == "https" else 80))
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))


def _check_resolution(host, port):
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UnsafeURL(f"Could not resolve host name: {host}")
    if not infos:
        raise UnsafeURL(f"Could not resolve host name: {host}")
    for info in infos:
        if is_ip_blocked(info[4][0]):
            raise UnsafeURL("Host resolves to a private or reserved address")


def normalize_url(url):
    """Canonical form used for de-duplication (not for fetching)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not TRACKING_PARAMS.match(k)])
    return urlunsplit((scheme, host, path, query, ""))


def get_domain(url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def same_site(a, b):
    da, db_ = get_domain(a), get_domain(b)
    return bool(da) and (da == db_ or da.endswith("." + db_) or db_.endswith("." + da))


def clean_text(value, maxlen=500):
    """Strip control characters and cap length. Output is still escaped by the UI."""
    if value is None:
        return ""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value)).strip()
    return text[:maxlen]


def valid_email(value):
    return bool(value) and len(value) <= 254 and bool(EMAIL_RE.match(value))
