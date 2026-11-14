"""Human check of the saved vault configuration (`check-vault-connection`).

Run as the broker user, usually through `secretops-configure --check` or
automatically after `secretops-configure` saves. The result is exactly one
of the fixed results below, each with a next step:

1. Without the token: an HTTP GET of the Infisical status endpoint at the
   configured URL. A connection, DNS, or timeout failure there means the
   vault is not reachable and the token is never sent. A TLS failure is
   not decided here (the CLI may trust other certificates); the CLI step
   decides it.
2. With the token, through the normal adapter: a by-name existence check of
   one fixed, synthetic name. "Missing" proves URL, token, project, and
   environment. Nothing is written and nothing is listed or printed.

Provider output is classified in the adapter and never printed.
"""

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from enum import Enum

from secretops.vaults import (
    VaultError,
    VaultNotFoundError,
    VaultTokenRefusedError,
    VaultUnreachableError,
)
from secretops.vaults.infisical_cli import InfisicalCliVault

CHECK_SECRET_NAME = "CONNECTION_CHECK_BY_SECRETOPS"
PROBE_TIMEOUT_SECONDS = 10
PROBE_MAX_BYTES = 64 * 1024


class CheckResult(Enum):
    UNREACHABLE = "unreachable"
    TOKEN_REFUSED = "token_refused"
    NOT_FOUND = "not_found"
    WORKING = "working"
    UNKNOWN = "unknown"


class Probe(Enum):
    CONFIRMED = "confirmed"  # the status endpoint answered with JSON
    UNREACHABLE = "unreachable"  # no connection at all
    INCONCLUSIVE = "inconclusive"  # something answered, or TLS failed


def probe_server(domain: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> Probe:
    """Ask the vault URL whether an Infisical API answers. Sends no token.

    Proxies from the environment are not used, as for the provider CLI,
    which gets no proxy variables. The answer is parsed only to see that it
    is a JSON object; it is never printed.
    """
    base = domain if domain.endswith("/api") else f"{domain}/api"
    request = urllib.request.Request(f"{base}/status", headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return Probe.INCONCLUSIVE
            body = response.read(PROBE_MAX_BYTES + 1)
    except urllib.error.HTTPError:
        return Probe.INCONCLUSIVE
    except urllib.error.URLError as error:
        # Raised while connecting: DNS, refused, unreachable, or timeout.
        if isinstance(error.reason, ssl.SSLError) or not isinstance(error.reason, OSError):
            return Probe.INCONCLUSIVE
        return Probe.UNREACHABLE
    except Exception:  # noqa: BLE001 - any other answer decides nothing
        return Probe.INCONCLUSIVE
    if len(body) > PROBE_MAX_BYTES:
        return Probe.INCONCLUSIVE
    try:
        data = json.loads(body)
    except ValueError:
        return Probe.INCONCLUSIVE
    return Probe.CONFIRMED if isinstance(data, dict) else Probe.INCONCLUSIVE


def check_vault_connection(
    env: str,
    *,
    probe: Callable[[str], Probe] | None = None,
    vault_factory: Callable[..., InfisicalCliVault] | None = None,
) -> tuple[CheckResult, str]:
    """Return the result and a safe detail (empty, or a secretops message)."""
    probe = probe or probe_server
    vault_factory = vault_factory or InfisicalCliVault
    try:
        connection = vault_factory().configured_connection()
    except VaultError as error:
        return CheckResult.UNKNOWN, str(error)
    answer = probe(connection.domain)
    if answer is Probe.UNREACHABLE:
        return CheckResult.UNREACHABLE, ""
    vault = vault_factory(api_confirmed=answer is Probe.CONFIRMED)
    try:
        vault.secret_exists(CHECK_SECRET_NAME, env)
    except VaultUnreachableError:
        return CheckResult.UNREACHABLE, ""
    except VaultTokenRefusedError:
        return CheckResult.TOKEN_REFUSED, ""
    except VaultNotFoundError:
        return CheckResult.NOT_FOUND, ""
    except VaultError as error:
        # Messages of VaultError are secretops' own fixed text.
        return CheckResult.UNKNOWN, str(error)
    return CheckResult.WORKING, ""


def result_lines(result: CheckResult, detail: str, env: str) -> list[str]:
    """Fixed text for each result, with the next step."""
    if result is CheckResult.WORKING:
        return [
            f"OK vault connection works: URL, token, project and environment {env} answered.",
            "A test name was looked up by name; nothing was written.",
            "A folder path that does not exist may look like an empty folder. To confirm the",
            "folder, check a name you know exists: sudo -u secretops-broker",
            f"  /usr/local/bin/secretops-broker check-vault-read --name NAME --env {env}",
        ]
    if result is CheckResult.UNREACHABLE:
        return [
            "FAIL vault not reachable at the configured URL (connection, DNS or TLS failed).",
            "Next: check the URL and that the vault server runs and is reachable from this",
            "machine; to change the URL run: sudo secretops-configure",
        ]
    if result is CheckResult.TOKEN_REFUSED:
        return [
            "FAIL the vault refused the access token (expired, revoked or wrong).",
            "Next: create a new access token for the machine identity and save it with:",
            "  sudo secretops-configure --token-only",
        ]
    if result is CheckResult.NOT_FOUND:
        return [
            "FAIL project, environment or folder not found, or not permitted for this machine identity.",
            f"Next: check the project ID and folder path (sudo secretops-configure), that environment {env}",
            "exists, and that the machine identity has access to this project and environment.",
            "To check another environment: sudo secretops-configure --check --env NAME",
        ]
    lines = ["FAIL could not tell what is wrong" + (f": {detail}" if detail else ".")]
    lines.append(
        "Next: check the URL, token, project ID and folder (sudo secretops-configure) and the "
        "vault server's logs; see docs/OPERATIONS.md, 'When something fails'."
    )
    return lines
