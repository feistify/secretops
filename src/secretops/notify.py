"""Tell the human that a plan waits for them, through ntfy.

A notification only says that a plan waits and gives its plan ID: no secret
names, environments, plan content, or link. It cannot approve anything; the
decision stays with the human's own broker access. A failed notification is
reported and audited without the topic URL or the token, and never stops
planning.

Settings come only from broker.env (the broker wrapper clears inherited
values): the topic URL, an optional access token, and an optional CA file
for an ntfy server behind a private CA. That CA file is separate from the
vault's, so neither replaces the other's trust.
"""

import ipaddress
import os
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from secretops.vaults.infisical_cli import ca_file_problem

NTFY_URL_KEY = "SECRETOPS_NOTIFY_NTFY_URL"
NTFY_TOKEN_KEY = "SECRETOPS_NOTIFY_NTFY_TOKEN"
NOTIFY_CA_FILE_KEY = "SECRETOPS_NOTIFY_CA_FILE"
NOTIFY_KEYS = (NTFY_URL_KEY, NTFY_TOKEN_KEY, NOTIFY_CA_FILE_KEY)
CHANNEL = "ntfy"
TIMEOUT_SECONDS = 5
# ntfy topic names: letters, digits, '-' and '_', at most 64 characters.
TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}")
TOKEN = re.compile(r"[!-~]{1,512}")

SENT = "sent"
# Fixed failure reasons, safe to print and audit: none names the URL,
# the topic, the token, or a server answer.
NOT_CONFIGURED = "not configured"
INVALID_SETTINGS = "invalid notification settings in broker.env"
TLS_FAILED = "TLS failed (certificate not trusted?)"
UNREACHABLE = "server unreachable"
TIMED_OUT = "timed out"
REDIRECTED = "the server answered with a redirect, which is not followed"
REFUSED = "refused by the server (check the token and the topic's access)"
SERVER_ERROR = "server error"
FAILED = "failed"


class NotifySettingsError(ValueError):
    """Safe to print: fixed text and key names only."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would send the token elsewhere: refuse it."""

    def redirect_request(self, *args, **kwargs):
        return None


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_ntfy_url(value: str) -> str:
    """https://host[:port]/topic; plain http only to this machine."""
    parts = urlsplit(value)
    try:
        parts.port
    except ValueError:
        raise NotifySettingsError("ntfy URL has an invalid port") from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or any(char.isspace() or not char.isascii() for char in value)
    ):
        raise NotifySettingsError("ntfy URL must be https://host[:port]/topic without credentials")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise NotifySettingsError("ntfy URL must use https unless the server is on this machine")
    topic = parts.path[1:] if parts.path.startswith("/") else ""
    if not TOPIC.fullmatch(topic):
        raise NotifySettingsError(
            "ntfy URL must end in one topic name: letters, digits, '-' and '_', at most 64"
        )
    return value


def validate_ntfy_token(value: str) -> str:
    if not TOKEN.fullmatch(value):
        raise NotifySettingsError("ntfy token must be one line of visible characters without spaces")
    return value


def is_configured(environ: Mapping[str, str]) -> bool:
    return bool(environ.get(NTFY_URL_KEY))


def settings(environ: Mapping[str, str]) -> tuple[str, str | None, str | None]:
    """URL, token, and CA file from the configuration, checked at every use."""
    url = validate_ntfy_url(environ.get(NTFY_URL_KEY, ""))
    token = environ.get(NTFY_TOKEN_KEY) or None
    if token is not None:
        validate_ntfy_token(token)
    ca_file = environ.get(NOTIFY_CA_FILE_KEY) or None
    if ca_file is not None and ca_file_problem(ca_file) is not None:
        raise NotifySettingsError(f"{NOTIFY_CA_FILE_KEY} is not a root-protected certificate file")
    if token is not None and urlsplit(url).scheme != "https" and not _is_loopback(urlsplit(url).hostname or ""):
        raise NotifySettingsError("ntfy token needs an https URL")
    return url, token, ca_file


def _opener(ca_file: str | None) -> urllib.request.OpenerDirector:
    # No proxy from the environment, as for the vault probe; no redirects.
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({}), _NoRedirect()]
    context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def send(
    message: str,
    environ: Mapping[str, str] | None = None,
    *,
    opener_factory: Callable[[str | None], urllib.request.OpenerDirector] = _opener,
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    """Send one message; return SENT or a fixed failure reason. Never raises."""
    environ = os.environ if environ is None else environ
    if not is_configured(environ):
        return NOT_CONFIGURED
    try:
        url, token, ca_file = settings(environ)
    except NotifySettingsError:
        return INVALID_SETTINGS
    headers = {"Title": "secretops", "Content-Type": "text/plain; charset=utf-8"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        opener = opener_factory(ca_file)
        with opener.open(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            return REDIRECTED
        if 400 <= error.code < 500:
            return REFUSED
        return SERVER_ERROR
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLError):
            return TLS_FAILED
        if isinstance(error.reason, TimeoutError):
            return TIMED_OUT
        if isinstance(error.reason, OSError):
            return UNREACHABLE
        return FAILED
    except TimeoutError:
        return TIMED_OUT
    except ssl.SSLError:
        return TLS_FAILED
    except Exception:  # noqa: BLE001 - a notification never stops planning
        return FAILED
    return SENT if 200 <= status < 300 else SERVER_ERROR


def waiting_message(plan_id: str, needed: str) -> str:
    return f"secretops: plan {plan_id} waits for {needed}"


TEST_MESSAGE = "secretops: test notification; no plan waits"
