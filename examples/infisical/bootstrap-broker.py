"""Set up a new self-hosted Infisical for secretops and connect the broker.

Run once, by a human, on the broker host after secretops is installed:

    sudo /opt/secretops/venv/bin/python -I bootstrap-broker.py --ca-file /etc/secretops-ca/infisical-root.pem

It asks for the first admin's email and password, then through Infisical's
API creates the admin and organization, a project with dev/staging/prod, the
machine identity secretops-broker (Token Auth, project member, no
organization role) and its access token, saves URL, project, CA file and
token in /opt/secretops/broker.env and runs the broker's vault check. No
password or token is printed, written elsewhere or passed on a command line.

Only for a vault that has no admin yet; otherwise it stops and changes
nothing. Needs secretops 0.1.14 or newer (it uses the broker's own
configuration code). Agents must not run it: it needs the admin password.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

from secretops import broker_config as bc

TOKEN_TTL = 30 * 24 * 3600  # seconds; renew with: sudo secretops-configure --token-only
TIMEOUT = 30
EMAIL = re.compile(r"[^@\s]+@[^@\s]+")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}")
MIN_PASSWORD = 14


class Stop(Exception):
    """Safe to print: fixed text, names and HTTP status codes only."""


class Vault:
    def __init__(self, url: str, ca_file: str | None) -> None:
        self.url = url
        self.context = ssl.create_default_context(cafile=ca_file)
        self.token: str | None = None

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=TIMEOUT) as response:
                text = response.read()
        except urllib.error.HTTPError as error:
            # The body is not printed: it can echo the request.
            raise Stop(f"{method} {path} failed with HTTP {error.code}") from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            kind = "TLS" if isinstance(reason, ssl.SSLError) else "connection"
            raise Stop(f"vault not reachable at {self.url} ({kind} failed)") from None
        try:
            return json.loads(text) if text else {}
        except ValueError:
            raise Stop(f"{method} {path} did not answer with JSON") from None


def field(data: dict, *keys: str) -> str:
    value: object = data
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    if not isinstance(value, str) or not value:
        raise Stop(f"unexpected answer from the vault (no {'.'.join(keys)})")
    return value


def ask_email() -> str:
    email = input("Admin email: ").strip()
    if not EMAIL.fullmatch(email):
        raise Stop("that is not an email address; nothing was changed")
    return email


def ask_password() -> str:
    first = bc.hidden_input(f"Admin password (hidden, at least {MIN_PASSWORD} characters): ")
    if len(first) < MIN_PASSWORD or not re.search(r"[^\W\d_]", first) or not re.search(r"[\d\W_]", first):
        raise Stop(f"the password needs {MIN_PASSWORD} characters, a letter and a number or symbol; nothing was changed")
    if bc.hidden_input("Admin password again: ") != first:
        raise Stop("the passwords differ; nothing was changed")
    return first


def name(value: str, what: str) -> str:
    if not NAME.fullmatch(value):
        raise Stop(f"{what} may use letters, digits, space, '_', '.', '-' (up to 64)")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bootstrap-broker.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="vault URL (default: the broker's configured URL)")
    parser.add_argument("--ca-file", metavar="PATH",
                        help="PEM file with the vault's private CA (default: the broker's configured one)")
    parser.add_argument("--organization", default="secretops", help="organization name (default: secretops)")
    parser.add_argument("--project", default="secretops", help="project name (default: secretops)")
    args = parser.parse_args(argv)
    step = "start"
    try:
        if os.geteuid() != 0:
            raise Stop("run with sudo from your own terminal, not from an agent session")
        if not bc.is_interactive():
            raise Stop("an interactive terminal is required")
        account = pwd.getpwnam(bc.BROKER_USER)
        values = bc.read_env(bc.BROKER_ENV, account.pw_uid)
        if bc.is_configured(values):
            raise Stop("the broker already has a project and token; use sudo secretops-configure")
        url = bc.validate_domain(args.url or values.get("INFISICAL_DOMAIN", ""))
        if not url.startswith("https://"):
            raise Stop("the vault URL must use https:// (the admin password and token travel to it)")
        ca_file = args.ca_file or values.get(bc.CA_FILE_KEY) or None
        if ca_file:
            ca_file = bc.validate_ca_file(ca_file)
        organization = name(args.organization, "--organization")
        project_name = name(args.project, "--project")

        vault = Vault(url, ca_file)
        config = vault.call("GET", "/api/v1/admin/config").get("config", {})
        if config.get("initialized") is not False:
            raise Stop("this vault already has an admin; nothing was changed. Create the project, "
                       "identity and token in its web page (docs/PREPARE.md), then run sudo secretops-configure")
        print(f"Vault {url}: new, no admin yet.")
        email = ask_email()
        password = ask_password()

        step = "admin"
        boot = vault.call("POST", "/api/v1/admin/bootstrap",
                          {"email": email, "password": password, "organization": organization})
        password = ""
        vault.token = field(boot, "identity", "credentials", "token")
        bootstrap_identity = field(boot, "identity", "id")
        org_id = field(boot, "organization", "id")
        print(f"Admin {email} and organization {organization}: created.")

        step = "project"
        created = vault.call("POST", "/api/v2/workspace", {"projectName": project_name, "organizationId": org_id})
        project = created.get("project") or created.get("workspace") or {}
        project_id = bc.validate_project_id(field(project, "id"))
        environments = sorted(e.get("slug", "") for e in project.get("environments", []))
        print(f"Project {project_name} ({project_id}): created, environments {', '.join(environments)}.")

        step = "identity"
        identity = vault.call("POST", "/api/v1/identities",
                              {"name": "secretops-broker", "organizationId": org_id, "role": "no-access"})
        identity_id = field(identity, "identity", "id")
        vault.call("POST", f"/api/v1/auth/token-auth/identities/{identity_id}", {
            "accessTokenTTL": TOKEN_TTL, "accessTokenMaxTTL": TOKEN_TTL, "accessTokenNumUsesLimit": 0,
            "accessTokenTrustedIps": [{"ipAddress": "0.0.0.0/0"}, {"ipAddress": "::/0"}]})
        vault.call("POST", f"/api/v2/workspace/{project_id}/identity-memberships/{identity_id}", {"role": "member"})
        print("Machine identity secretops-broker: Token Auth, member of the project.")

        step = "token"
        issued = vault.call("POST", f"/api/v1/auth/token-auth/identities/{identity_id}/tokens", {"name": "broker"})
        token = bc.validate_token(field(issued, "accessToken"))
        expires = time.strftime("%Y-%m-%d", time.localtime(time.time() + TOKEN_TTL))

        step = "save"
        updated = dict(values)
        updated["INFISICAL_DOMAIN"] = url
        updated["SECRETOPS_INFISICAL_PROJECT_ID"] = project_id
        updated.setdefault("SECRETOPS_INFISICAL_PATH", "/")
        updated[bc.TOKEN_KEY] = token
        if ca_file:
            updated[bc.CA_FILE_KEY] = ca_file
        bc.write_env(bc.BROKER_ENV, updated, account.pw_uid, account.pw_gid)
        token = ""
        print(f"Broker access token: created and saved in {bc.BROKER_ENV}, not displayed; expires about {expires}.")

        step = "cleanup"
        try:
            vault.call("DELETE", f"/api/v1/identities/{bootstrap_identity}")
            print("Bootstrap identity: deleted.")
        except Stop:
            print("NOTE: delete the bootstrap machine identity in the web page "
                  "(Organization, Access Control, Machine Identities); it has full admin rights.")
        vault.token = None
    except Stop as error:
        print(f"bootstrap-broker: {error}", file=sys.stderr)
        if step not in ("start", "admin", "cleanup"):
            print(f"bootstrap-broker: stopped at '{step}'. The admin exists; finish in the web page "
                  "(docs/PREPARE.md) and run sudo secretops-configure.", file=sys.stderr)
        return 1
    except bc.ConfigError as error:
        print(f"bootstrap-broker: {error}", file=sys.stderr)
        return 1
    except KeyError:
        print(f"bootstrap-broker: broker user {bc.BROKER_USER} does not exist; install secretops first",
              file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nbootstrap-broker: cancelled", file=sys.stderr)
        return 2
    print("Next: log in to the web page as the admin and keep the password in your password manager.")
    code = bc.run_check(account, bc.DEFAULT_CHECK_ENV, bc.check_timeout(updated))
    if code != 0:
        print("The configuration was saved, but the vault check failed; "
              "check again: sudo secretops-configure --check")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
