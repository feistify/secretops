# Broker host model

This document describes how to install secretops as a **broker** on any host, and how to give an agent governed access to it over SSH. It is host-agnostic: the same layout works on a NAS, a VPS, a home server, or a laptop. Only the way you get Python or a container runtime onto the box differs.

## Roles and the colocation rule

Three roles, which may or may not share a machine:

- **Vault host** — where Infisical (cloud or self-hosted) runs. The broker only needs network reach to it.
- **Broker host** — where the secretops CLI runs. It holds the vault machine-identity credentials, the plan store, the audit log, and the policy file. This is the trust anchor.
- **Agent host** — where the coding agent runs. It talks to the broker over SSH and never holds vault credentials or secret values.

The rule: **the broker must live in the same privilege domain as the local secret material it operates on** (for example the source file of `vault.secret.import_file`), and **the agent must live in a different privilege domain**. Usually those domains are separate machines, but a separate Unix user on a shared machine works too — see the same-machine variant below. The vault can be anywhere reachable. Control plane (recipes, plan ids, approvals, statuses) crosses domain boundaries; secret values do not.

If secret material lives on several hosts, install one broker per host. Broker installs are cheap and identical.

## Broker host layout

```
/opt/secretops/
  broker.env       # 0600, owner: broker user — Infisical credentials and deployment settings
  policy.yaml      # policy for this broker (or omit to use the packaged default)
  recipes/         # agent-uploaded recipe YAML (names and metadata only, never values)
  plans/           # saved plan artifacts
  audit/           # audit log
```

`broker.env`:

```sh
INFISICAL_DOMAIN=http://localhost:8086          # the vault this broker talks to
INFISICAL_TOKEN=...                             # machine identity token, human-provisioned
SECRETOPS_INFISICAL_PROJECT_ID=...              # project this broker reads and writes
SECRETOPS_INFISICAL_PATH=/                      # folder path inside the project
SECRETOPS_INFISICAL_BIN=/usr/local/bin/infisical  # root-owned CLI the broker runs
SECRETOPS_GH_BIN=/usr/bin/gh                    # root-owned gh the broker runs (GitHub workflows)
SECRETOPS_PLAN_DIR=/opt/secretops/plans
SECRETOPS_AUDIT_PATH=/opt/secretops/audit/secretops.jsonl
SECRETOPS_POLICY_PATH=/opt/secretops/policy.yaml
```

`INFISICAL_TOKEN`, `SECRETOPS_INFISICAL_PROJECT_ID`, and `SECRETOPS_INFISICAL_PATH` are required for calls to the vault. Saving a vault plan needs the project and folder but no token; there is no fallback to `infisical login` or `.infisical.json`. `SECRETOPS_INFISICAL_BIN` is optional but recommended: without it, secretops looks for `infisical` only in `/usr/local/bin`, `/usr/bin`, and `/bin`, never in the caller's `PATH`. The CLI receives the token, so its executable and directory must be administrator-managed and not writable by the agent or broker user. `broker.env` is broker-owned and must not be readable or writable by the agent. `SECRETOPS_PROVIDER_TIMEOUT_SECONDS` (default 60) bounds each Infisical and `gh` call; an invalid value stops a plan before any secret is read.

Plans record the vault destination (`INFISICAL_DOMAIN`, `SECRETOPS_INFISICAL_PROJECT_ID`, `SECRETOPS_INFISICAL_PATH`) when they are saved. Changing the resolved vault destination in `broker.env` stops open plans that use the vault at preflight and execution; GitHub-only metadata verification is independent of vault configuration. For vault operations, plan and approve them again for the new destination. Replacing `INFISICAL_TOKEN` for the same destination does not affect open plans.

Pinning `SECRETOPS_PLAN_DIR`, `SECRETOPS_AUDIT_PATH`, and `SECRETOPS_POLICY_PATH` is required, not optional. The default plan and audit paths are cwd-relative; the default policy is a packaged resource. Unpinned plan/audit paths fragment state, and an agent-writable plan directory would allow status tampering that bypasses approval. Here the plan store is owned by the broker user and the agent never gets a shell, so the approval gate is enforced by filesystem ownership and the SSH boundary.

## Exit codes of `execute-plan`

`execute-plan` exits 0 on success, 1 on a failure whose outcomes are known, and 3 when any write outcome is unknown: a vault write without a matching read-back, or a GitHub write that started but was not acknowledged. Exit 3 means stop and let a human investigate; never retry automatically. Earlier versions exited 1 for unknown outcomes too, so scripts that treat every failure as 1 must handle 3. Saved plans and approvals are unaffected by this change.

A stopped GitHub sync reports every mapping as acknowledged, unknown, not written, or not attempted, in output and in the audit log; acknowledged writes are not rolled back. If the audit log cannot be written, the run stops further writes, prints what it knows, and says that the audit record is incomplete.

## Upgrading to destination-bound plans

After upgrading from a version without destination binding, all existing plans without `contract_version` require replanning and, for approval-required actions, fresh human approval. They remain inspectable but cannot be approved or executed. Preserve the plan store and audit during upgrades; do not add destination fields to old plans to reuse their approvals. Plans with an unsupported contract version are also refused.

## Bootstrap (human steps, once per broker host)

For a broker on the same machine as the agent, `scripts/install.sh` performs these steps; see [INSTALL.md](INSTALL.md). The steps below describe the model and a manual setup on another host.

1. **Create a dedicated broker user** (for example `secretops-broker`). On appliance systems, create it through the admin UI.

2. **Install the secretops CLI** into an administrator-managed environment and run it as the broker user. The broker must not be able to replace application code or executables. Example installation mechanisms (check ownership and parent permissions afterward):
   - `uv tool install secretops-<version>-py3-none-any.whl` (host has Python >= 3.11 or uv can provision it)
   - a virtualenv with `python3` version 3.11 or newer: `python3 -m venv /opt/secretops/venv && /opt/secretops/venv/bin/pip install <wheel>`
   - a container image with the wheel installed, for hosts without a usable Python. Mount `/opt/secretops` and read-only mounts for any import source files.

3. **Create the layout** above with split ownership: `/opt/secretops` and application/executable/manifest/policy paths must be administrator-owned, with no parent writable by the broker or agent. Only `broker.env` (0600), the broker home, and state directories such as plans/audit/recipes (0700) belong to `secretops-broker`. Do not make the entire application tree broker-owned. The proposed [managed installation design](design/local-broker-installation.md) moves configuration/token/state into separate FHS paths; that new installer is not implemented yet.

4. **Provision vault credentials.** Create a machine identity in the target vault, scoped to the intended project/environments. Put its access token into `broker.env` as `INFISICAL_TOKEN` yourself, together with the project ID and folder path. Token renewal is manual for now; an expired token fails safely and never falls back to a user login. The agent must never see, fetch, or transport these credentials — do not store them in another vault for the agent to "pick up".

5. **Grant read access to import sources.** For `vault.secret.import_file`, the broker user needs read permission on the source file (same owner, group, or ACL). Keep source files at mode 0600; secretops refuses broader permissions.

6. **Install the wrappers** from `scripts/`:
   - `/usr/local/bin/secretops-broker` — loads `broker.env`, execs secretops. Root-owned, 0755.
   - `/usr/local/bin/secretops-agent` — SSH forced-command wrapper for agent keys. Root-owned, 0755.

7. **Add SSH keys** to `~secretops-broker/.ssh/authorized_keys`:

   ```
   # agent key: allowlisted subcommands only, no shell, no scp, no forwarding
   command="/usr/local/bin/secretops-agent",restrict ssh-ed25519 AAAA... agent

   # human key: unrestricted (or omit and use your own account + sudo)
   ssh-ed25519 AAAA... human
   ```

8. **Do not let sshd import client environment.** The wrappers hardcode their
   security-relevant paths and do not read them from the environment, but keep
   `sshd_config` clean as defense in depth: no `AcceptEnv SECRETOPS_*` (or a
   broad `AcceptEnv *`), and `PermitUserEnvironment no` (the default). The
   `restrict` option on the agent key already disables the main env-passing
   channels.

## GitHub authentication (human only)

GitHub workflows also require `gh` on the broker and authentication available to the dedicated broker user. Your browser or your own Unix account being logged into GitHub does not authenticate the broker user. Provision only the repository permissions needed: secret writes require the target's environment-secret write permission; verification needs read access to environment-secret metadata. Account permissions and organization policies must also allow access to the intended repository/environment.

For a human-operated setup using a CLI login, run from a directory the broker user can access:

```sh
cd /tmp
sudo -u secretops-broker gh auth login --hostname github.com
```

Choose HTTPS and browser login. If automatic browser launch fails, open `https://github.com/login/device` yourself and enter the one-time code privately. Do not send the code or a token to an agent or chat. CLI login state belongs to the broker user's protected home, not to `broker.env`. secretops uses only this stored login: `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, and `GITHUB_ENTERPRISE_TOKEN` are never passed to `gh`, even when they are set in `broker.env`. Do not configure authentication under the agent's account.

A human can check the broker user's CLI login without displaying provider output or loading `broker.env`. This checks the login configured above, not a token supplied only through broker configuration:

```sh
cd /tmp
sudo -u secretops-broker sh -c '
  unset INFISICAL_TOKEN GH_HOST GH_REPO GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN
  if gh auth status --hostname github.com >/dev/null 2>&1; then
    echo "PASS: GitHub authentication"
  else
    echo "FAIL: GitHub authentication"
  fi
'
```

secretops runs `gh` with an explicit environment, never the broker's own:

- `gh` is `SECRETOPS_GH_BIN` when set (an absolute path to an executable), otherwise the first `gh` in `/usr/local/bin`, `/usr/bin`, and `/bin`. The caller's `PATH` is never searched, and a missing `gh` stops the plan before any secret is read. The executable and its directory must be administrator-managed and not writable by the agent or broker user.
- `gh` receives only `HOME`, `XDG_CONFIG_HOME` and `GH_CONFIG_DIR` (when set), `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` (when set, for a keyring-stored login), `SSL_CERT_FILE`, `SSL_CERT_DIR`, the `HTTP(S)_PROXY`/`NO_PROXY` variables in either case, `LANG`, and `LC_ALL`. The broker sets `PATH` to the fixed directories above, `GH_PROMPT_DISABLED=1`, `GH_NO_UPDATE_NOTIFIER=1`, and `NO_COLOR=1`.
- GitHub tokens, `GH_HOST`, `GH_REPO`, `INFISICAL_TOKEN`, and every other broker variable are dropped.

`SECRETOPS_GH_BIN` may come only from `broker.env`: `secretops-broker` clears an inherited value before loading it. When upgrading secretops, reinstall `/usr/local/bin/secretops-broker` from the same version.

**Migrating from a token in `broker.env`.** Earlier versions passed the whole broker environment to `gh`, so a `GH_TOKEN` or `GITHUB_TOKEN` in `broker.env` authenticated it. After upgrading, such a broker sees `gh` as not logged in and GitHub plans fail. Log in as the broker user with `gh auth login` (above), run the login check, then remove the GitHub token from `broker.env`. If `gh` is not in the fixed directories, set `SECRETOPS_GH_BIN`.

For an enterprise deployment, use the planned GitHub host instead of `github.com`. An authentication check does not prove repository permissions; follow it with a metadata-only `github.env_secret.verify_many` saved plan through the broker. Writes require a separately approved plan. GitHub writes are bounded by the provider timeout but have no read-back: GitHub does not return secret values. A write is "acknowledged" when `gh` exits successfully, and "unknown" after a nonzero exit, a signal, a timeout, or a failure after `gh` started; do not infer value equality from metadata presence.

These provider commands are human setup steps, not additions to the agent command allowlist. For a local broker, [INSTALL.md](INSTALL.md) and [OPERATIONS.md](OPERATIONS.md) give the maintained installation, renewal, and upgrade procedures.

## Operating flow

Give agents the configured broker connection and the consumer-project instruction template in [AGENT_USAGE.md](AGENT_USAGE.md#instructions-for-agents-in-consumer-projects). Agents do not automatically discover this deployment. The interface is CLI over restricted SSH; no HTTP API or MCP server is provided.

Agent (over its restricted key). Recipes are referenced by bare name; the
wrapper resolves them inside the recipe directory, so the agent cannot name an
arbitrary path:

```sh
ssh secretops-broker@host write-recipe import-env.yaml < import-env.yaml
ssh secretops-broker@host secretops plan --file import-env.yaml --save
ssh secretops-broker@host secretops show-plan --id <plan_id>
ssh secretops-broker@host secretops preflight-plan --id <plan_id>
# blocked until a human approves:
ssh secretops-broker@host secretops execute-plan --id <plan_id>
```

The agent wrapper accepts each subcommand only in an exact shape: `plan --file
<name>.yaml --save` (name resolved under the recipe directory, no slashes or
`..`), and `show-plan`/`preflight-plan`/`execute-plan --id <plan_id>` with a
validated id. Anything else — an absolute `--file` path, extra arguments, an
unlisted subcommand — is denied before the broker is invoked.

Human (over an unrestricted key):

```sh
ssh secretops-broker@host /usr/local/bin/secretops-broker show-plan --id <plan_id>
ssh secretops-broker@host /usr/local/bin/secretops-broker approve-plan --id <plan_id>
```

The agent wrapper allows: `version`, `actions`, `plan`, `show-plan`, `preflight-plan`, `execute-plan`, and `write-recipe`. It does not allow `approve-plan` or `reject-plan`: approval is human-only, enforced at the SSH transport layer rather than by convention.

The human broker endpoints `prepare-add`, `review-add`, and `submit-add` used by `secretops add` are not on this list either; see [Human secret entry](#human-secret-entry).

`write-recipe` exists because `restrict` disables scp/sftp. It reads YAML from stdin into `/opt/secretops/recipes/` under `umask 077` (files land at 0600), with a filename allowlist and a size cap; oversize input is rejected rather than truncated. Recipes contain secret names and metadata only, never values; the plan/approve lifecycle remains the gate on what actually executes.

## Human secret entry

`secretops add` stores one new secret that a human has, for example an API key created in a provider console. The human runs it on their own computer; it reaches the broker through the human's existing broker access, never through the agent key:

```text
$ secretops add SCALEWAY_API_KEY --env staging

Broker:      nas
Vault:       infisical / <project id> / staging / /
Server:      https://vault.example.test
Secret:      SCALEWAY_API_KEY
Operation:   create only; refused if present at the final check
Plan:        <plan id>

Checks passed. The secret is currently absent.
Value (hidden):
Secret:      SCALEWAY_API_KEY
Operation:   create only; refused if present at the final check
Save? [y/N]: y

Saved and verified.
```

`secretops add` with no arguments asks for the name and environment. When an agent has prepared a `vault.secret.add` plan, the human runs `secretops add --plan <plan id>`. Exit codes: 0 saved and verified; 1 nothing was written (refused, already exists, provider error, invalid input); 2 cancelled; 3 outcome unknown.

**Input options.** Type or paste one nonblank line into the hidden prompt, then press Enter. The maximum value size is 16 KiB in UTF-8. Unicode and meaningful leading/trailing spaces are preserved; empty/whitespace-only values, multiline content, and control characters are rejected. There is no `--value`, value environment variable, file input, or raw-stdin input for this command. The endpoint's internal stdin protocol is not a user input mode. Name and environment may be supplied as arguments or answered interactively; `--plan` cannot be combined with them. Use `--broker <name>` when selecting among configured brokers. Ctrl+C before submission or `n` at confirmation cancels without sending the value; interruption after submission may leave an unknown outcome, which must be checked before retrying.

**One-time client setup.** Install the same secretops version on the human's computer (it needs no vault token or provider CLI) and describe the broker access in `~/.config/secretops/client.yaml` (or the file named by `SECRETOPS_CLIENT_CONFIG`):

```yaml
default_broker: nas
brokers:
  nas:
    ssh: secretops-broker@nas          # human key on the broker user; ~/.ssh/config applies
    # broker_command: /usr/local/bin/secretops-broker   (default)
  nas-personal:
    ssh: alice@nas                     # your own account on the broker host ...
    sudo_user: secretops-broker        # ... then sudo -n to the broker user
  laptop:
    sudo_user: secretops-broker        # local mini-broker through your own sudo
```

With several brokers and no `default_broker`, `--broker <name>` is required. The client runs fixed commands: `ssh -T` without a remote PTY and with agent/X11/port forwarding off, host-key checking as configured in your SSH settings, then `/usr/local/bin/secretops-broker prepare-add|review-add|submit-add` with validated arguments. A second human key is not required. The three supported routes:

- **SSH as the broker user** (`ssh:` alone): the existing unrestricted human key in the broker user's `authorized_keys`.
- **SSH to your own account, then sudo** (`ssh:` with `sudo_user:`): the client runs `sudo -n -u <broker user> -- /usr/local/bin/secretops-broker ...` on the broker host. Without a remote PTY sudo cannot ask for a password, so `-n` makes it fail instead of hanging; give your account a narrow rule for exactly these endpoints (edit with `visudo -f /etc/sudoers.d/secretops-add`):

  ```text
  alice ALL=(secretops-broker) NOPASSWD: /usr/local/bin/secretops-broker prepare-add *, /usr/local/bin/secretops-broker review-add *, /usr/local/bin/secretops-broker submit-add
  ```

  The arguments are validated again by the broker. Do not grant passwordless sudo to the broker user in general.
- **Local sudo** (`sudo_user:` alone, broker on the same machine): sudo may ask for your password on the terminal during the review step, before the value prompt; it never reads it from the request stream.

Each route stays as protected as you keep that credential. A `command: [...]` entry runs a local command as your own user and is meant for development only.

**Terminal requirement.** Before asking for the value, `secretops add` enables bracketed paste and asks the terminal to confirm it (a DECRQM report for mode 2004). If the terminal does not answer within about 1.5 seconds, or reports the mode off, entry is refused before the value prompt: without paste markers a multiline paste would end the value at its first line and its next line would reach the confirmation prompt. Support for this report varies between terminal emulators and versions. The actual local T14 session passed the support query, single-line paste, multiline rejection, cancellation, and decline checks; it reported `VTE_VERSION=6800`, but its exact emulator name/version remains unconfirmed. tmux, screen, and SSH-into-client combinations have not been validated. Test your combination with synthetic input first; see [the validation record](validation/interactive-secret-entry.md).

**What happens.** The broker saves (or loads) the plan, checks the destination binding, policy, connection, and that the name is absent, and returns its validated plan and digest; only then is the value requested. The value is read from the terminal with echo off and sent once, inside a JSON request on the endpoint's stdin, never in arguments, the environment, or client files. The broker checks the digest the human confirmed, the destination, policy, and plan status again under a per-plan lock, records the approval with that digest, creates the secret, and confirms it by reading it back. The value is never printed, logged, or written to the plan or audit.

**Limits.**

- Infisical has no create-only write (CLI v0.43.99 `secrets set` is an upsert; see the design). A secret created by someone else between the final check and the write is overwritten. Re-inspect this if you upgrade the Infisical CLI.
- Multiline values are not supported: a multiline paste is rejected as a whole. Terminals that do not confirm bracketed paste are refused (see the terminal requirement above). Ctrl+C cancels at any point before the value is sent, including in the middle of a paste.
- The default policy denies `vault.secret.add` in production; confirmation does not override it.
- If the agent runs under the same OS account as the human, it may be able to use the human's SSH agent, keys, or cached sudo. This release does not claim isolation in that case. Hardware-backed keys that require touch, or per-use SSH-agent confirmation, strengthen the access but do not prove that a human reviewed a specific plan.
- `approve-plan` refuses `vault.secret.add` plans (approval happens with the value), and `execute-plan` refuses them without changing their status. `reject-plan` works as usual.
- An "outcome unknown" result means the write may have happened. Do not submit again until you have checked the plan with `show-plan` and, if needed, the secret.

## Variant: same machine as the agent (local mini-broker)

When the secret file lives on the machine the agent also runs on (for example a developer laptop), the boundary that matters is the privilege domain, not the machine. If the broker runs under the agent's own user, the agent can read the source file and `broker.env` directly, and every guarantee in this document degrades to convention.

Two honest configurations:

- **Human-only use, no agent involved.** If you run `secretops plan/approve-plan/execute-plan` yourself, no boundary is needed — you already know the values. You still need a machine identity token and the project settings; secretops does not use your `infisical login` session. Use your own identity, not a copy of a broker's. secretops still buys you: no values in shell history, plan files, or audit; secret-name validation; the `create_only` guard; an audit trail.
- **Agent-driven use.** Create a second Unix user as a local mini-broker with the exact layout from this document. The source file and `broker.env` belong to that user, mode 0600. The agent connects with `ssh secretops-broker@localhost` using the same forced-command `authorized_keys` entry. Same scripts, same allowlist, zero new code — the SSH boundary works identically on `localhost`.

Do not run an agent-driven import under the agent's own account and call it governed. It is a workflow gate at best; if that is acceptable for a low-stakes personal case, say so explicitly in the task instead of implying enforcement that is not there.

## Variant: ephemeral broker for one-shot migrations

For a one-time pickup of a secrets file from some VM, a permanent broker install is unnecessary ceremony. Run the broker ephemerally on that host:

1. Create a **short-TTL machine identity** in the target vault, scoped to only the target project/environment.
2. On the source host, run the CLI without installing: `uvx --from secretops-<version>-py3-none-any.whl secretops ...`, or a short-lived container. Pin `SECRETOPS_PLAN_DIR`, `SECRETOPS_AUDIT_PATH`, and the vault credentials to a 0700 temp workdir.
3. Run the normal lifecycle (`plan --save`, human `approve-plan`, `preflight-plan`, `execute-plan`) over SSH, same trust properties as a permanent broker.
4. Before teardown, copy the plan artifacts and audit log back for record-keeping — they contain names, statuses, and metadata only, never values, so they are safe to transport through the agent.
5. Delete the temp workdir and revoke the machine identity.

The trust properties are identical to a permanent broker; only the lifetime differs. What this variant deliberately avoids is the tempting shortcut of copying the secrets file to wherever a broker already exists — move the broker to the data, even for one afternoon.

## What this enforces vs. what stays convention

Enforced by this setup:

- vault credentials exist only on the broker host, unreadable by the agent
- the agent cannot obtain a shell, copy files, or forward ports on the broker host
- the agent cannot approve or reject plans
- the agent cannot edit plan artifacts, the audit log, or policy (filesystem ownership)
- secret values never transit the agent host; for imports where source and vault share the broker host, values never leave that machine at all

Still convention (see `AGENT_USAGE.md`):

- the agent's conduct on its *own* host (not reading local `.env` files, tokens, or credentials there)
- the quality of human review at `show-plan`/`approve-plan` time

## Limitations

- The forced-command wrapper splits `SSH_ORIGINAL_COMMAND` on whitespace without shell quoting rules. Arguments must not contain spaces or quotes; secretops subcommands, plan ids, and recipe filenames never need them. Shell metacharacters are rejected outright.
- One vault domain per `broker.env`. A broker talks to one vault; cross-vault flows are separate plans on separate brokers.
- Plan store and audit log are per broker host. A unified control plane across brokers is future work (see the remote broker boundary item in `TODO.md`).
