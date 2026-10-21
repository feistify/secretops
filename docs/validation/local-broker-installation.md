# Local broker installation validation

Date: 2026-10-19
Installer: `scripts/install.sh` from the commit that adds this record (79bd079 plus the sshd start wait found below)
Broker wheel SHA-256: `06a6351d7d9a1ef7beef7cc25ff98ab551372e6a5fc04f5c70f5478f47100f9f` (application code of 79bd079)
System: Ubuntu 24.04.5 cloud image, KVM guest; Python 3.12.3, OpenSSH 9.6p1, Infisical CLI 0.44.2 and gh 2.102.0 from their official apt repositories
Vault: self-hosted Infisical on the local network over plain HTTP, a new test project and a project machine identity with token auth
GitHub: a temporary private test repository, environment `dev`, deleted after the run

## Setup

A clean VM with two accounts: `human` (sudo) and `agent` (no sudo). The agent prepared the VM, installed the prerequisites as root, and ran the installer from the `human` account with `--agent-user agent`. Before any credential was entered, the human replaced the setup key on the `human` account with their own; from then on the agent could log in only as `agent`. The human entered the vault token, did the GitHub device login, stored the test value, and approved the plan in their own SSH session. The agent used only `secretops-local`.

## Results

| Step | Who | Result |
| --- | --- | --- |
| First installer run | agent, as `human` | Failed at the last check: `systemctl enable --now` returned before sshd listened, so the agent check got `Connection refused`. Fixed by waiting up to 10 s for the port; the VM was recreated from a clean disk |
| Installer on the clean disk | agent, as `human` | Completed; `PASS: agent reaches the broker through the restricted SSH key` |
| Agent boundary | agent | `version`, `actions` allowed; `approve-plan`, shell command, `-tt`, reading `broker.env`, listing plans, `sudo` refused |
| Reboot | agent | Service active after reboot, listening on `127.0.0.1:22222` only; agent reaches the broker |
| `sudo secretops-configure` | human | Vault URL, project ID, folder and hidden token accepted; unencrypted HTTP warning shown |
| `check-vault-read` with a `SECRETOPS_` name | human | `reserved secret name`, before any vault call (expected; the name was a poor suggestion) |
| `check-vault-read` with an ordinary existing name | human | `OK secret exists` |
| `gh auth login` as the broker, from `cd /` | human | Device login; no browser or clipboard in the VM, so the code was entered on the human's machine |
| `secretops add DEMO_API_KEY --env dev` | human | Destination shown, hidden input, confirmation, `Saved and verified.` |
| Recipe upload, plan, preflight | agent | Plan saved; preflight passed, source present, no GitHub call |
| `show-plan`, `approve-plan` | human | Plan reviewed and approved |
| `execute-plan` | agent | Exit 0, `OK GitHub writes acknowledged: 1` |
| `verify_many` plan and execute | agent | Exit 0, `present names: DEMO_API_KEY`, none missing |
| Execute the same sync plan again | agent | Refused, exit 1 |

No secret value, token or device code was given to the agent. All values were synthetic.

## Findings

- **Fixed:** the installer checked the agent connection before the `Type=simple` sshd service was listening.
- **Fixed in docs:** `docs/INSTALL.md` now names the reserved secret prefixes at the first vault check.
- **Fixed in PR #11 (42d6687), not in the installer:** an executed plan is now refused before any other check and keeps its status. Original observation: refusing to execute a plan that is not approved sets its status to `failed`, even when it was `executed`. After the replay attempt above, the executed sync plan shows `failed`; the audit log still has the successful execution. A `failed` plan cannot be approved again, so this does not allow a replay, but any agent call can rewrite the recorded outcome of an executed plan. The refusal message (`requires approval`) also does not say that the plan was already executed.
- The Infisical web UI copy buttons do not work over plain HTTP (the browser clipboard API needs HTTPS or localhost); the project ID can be taken from the address bar.

## Evidence limits

- One mapping, `dev` only. Upgrade and rollback, backup and restore, and token renewal were checked in the container test, not on this VM.
- The agent controlled the hypervisor and could have read the VM disk; it did not. The VM's `human` account had passwordless sudo, which a real installation should not need.
- The GitHub value was not compared with the vault value: GitHub does not return secret values.
