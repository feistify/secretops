# secretops

secretops is a CLI-first broker for developers who want coding agents to manage secret operations without seeing secret values.

Humans enter credentials and approve writes; agents prepare plans and execute approved operations through a restricted SSH wrapper. Store an API key in Infisical, then let an agent sync it to GitHub through a separately approved plan.

![The human enters a value with secretops add, the agent saves a plan to sync it to a GitHub environment, the human approves it, and the agent executes it and verifies the secret name](https://raw.githubusercontent.com/feistify/secretops/main/docs/images/flow.png)

**v0.1.2, early-stage software.** The supported installation path is a local broker on Ubuntu 24.04. Python 3.11 or newer is required; the clean-VM installation was tested with Python 3.12.3, Infisical CLI 0.44.2 and GitHub CLI 2.102.0. See the [validation record and its limits](https://github.com/feistify/secretops/blob/main/docs/validation/local-broker-installation.md).

| Available now | Current limits |
| --- | --- |
| Hidden human entry, generation, rotation, import and environment copy in Infisical | One Infisical server, project and folder per broker; no OpenBao adapter yet |
| Saved plans, destination binding, human approval, safe audit and explicit write outcomes | Infisical create-only checks are not atomic; unknown writes are never retried automatically |
| GitHub environment secret sync and name-presence verification | GitHub cannot return stored values; production writes are denied by the default policy |
| Restricted agent SSH access and a Linux installer | Use a separate agent account for an enforced boundary; human entry over SSH and tmux remain untested |

secretops is not a vault. The vault remains the source of truth. secretops uses external vaults and targets through narrow adapters, and it works with secret names, environments, plan artifacts, policy decisions, approvals, audit events, and safe status messages.

secretops must never print secret values or persist them in plans, audit, or logs. Values are intentionally stored in the vault and approved targets; provider-required temporary files stay broker-private and are removed after use.

## Installation

To run secretops as a local broker for a coding agent on Linux, follow [docs/INSTALL.md](https://github.com/feistify/secretops/blob/main/docs/INSTALL.md): `scripts/install.sh` creates the broker account, the restricted agent SSH access and the human tools, and `secretops-configure` stores the vault token at a hidden prompt. Daily use, failures, token renewal, upgrade, backup and uninstall are in [docs/OPERATIONS.md](https://github.com/feistify/secretops/blob/main/docs/OPERATIONS.md).

Agents use the installed broker through `secretops-local`, never a source checkout of secretops or its credentials. [docs/BROKER_HOST.md](https://github.com/feistify/secretops/blob/main/docs/BROKER_HOST.md) explains the deployment model and other variants (remote broker host, ephemeral broker); [docs/AGENT_USAGE.md](https://github.com/feistify/secretops/blob/main/docs/AGENT_USAGE.md) explains the agent operating model and the consumer-project instructions.

For development, `uv run secretops ...` in this repository uses the configuration described below.

## Demo: enter once, sync through an approved plan

After completing [installation and authentication](https://github.com/feistify/secretops/blob/main/docs/INSTALL.md), use a test Infisical project and a test GitHub repository with a `dev` environment. The commands below are for the installed local broker. Enter a made-up, single-line value for this demo; never give it to the agent. Run commands one at a time and stop on any failure; exit 3 means the agent must ask the human to resolve an unknown write outcome before continuing.

**Human, in your own terminal:**

```sh
secretops add DEMO_API_KEY --env dev
# Your sudo password may be asked first, then the hidden value and Save? [y/N]
# Saved and verified.
```

**Agent:** create two metadata-only recipes in a temporary directory; no secretops checkout is needed. Set `owner` and `repo` below to the test repository's owner and name before running the block. Keep this agent shell open for the remaining commands:

```sh
demo_dir=$(mktemp -d)
owner=example-owner repo=example-repo
cat > "$demo_dir/demo-sync.yaml" <<YAML
action: github.env_secret.sync_many
env: dev
github_owner: $owner
github_repo: $repo
github_environment: dev
secrets:
  - source: DEMO_API_KEY
    target: DEMO_API_KEY
YAML
cat > "$demo_dir/demo-verify.yaml" <<YAML
action: github.env_secret.verify_many
github_owner: $owner
github_repo: $repo
github_environment: dev
secrets:
  - name: DEMO_API_KEY
YAML
```

Upload the sync recipe and prepare its plan:

```sh
secretops-local write-recipe demo-sync.yaml < "$demo_dir/demo-sync.yaml"
secretops-local plan --file demo-sync.yaml --save
secretops-local show-plan --id SYNC_PLAN_ID
secretops-local preflight-plan --id SYNC_PLAN_ID
```

Replace `SYNC_PLAN_ID` with the ID returned by `plan`. If preflight fails, stop and tell the human. The agent cannot approve its own plan.

**Human:** review the source, destination, GitHub repository and environment, then approve through your own broker access:

```sh
sudo -u secretops-broker /usr/local/bin/secretops-broker show-plan --id SYNC_PLAN_ID
sudo -u secretops-broker /usr/local/bin/secretops-broker approve-plan --id SYNC_PLAN_ID
```

![show-plan output for the sync plan: action, source environment, GitHub target, secret mapping, vault destination and plan digest, with secret_value_included False](https://raw.githubusercontent.com/feistify/secretops/main/docs/images/plan.png)

**Agent:** after approval, execute the sync and prepare a separate metadata-only verification plan:

```sh
secretops-local execute-plan --id SYNC_PLAN_ID
# OK GitHub writes acknowledged: 1
secretops-local write-recipe demo-verify.yaml < "$demo_dir/demo-verify.yaml"
secretops-local plan --file demo-verify.yaml --save
secretops-local preflight-plan --id VERIFY_PLAN_ID
secretops-local execute-plan --id VERIFY_PLAN_ID
# present names: DEMO_API_KEY
```

Replace `VERIFY_PLAN_ID` with the second plan's ID. Verification needs no approval under the default policy and confirms only that the name exists, not value equality. The human deletes the demo secrets from Infisical and GitHub when finished. See [OPERATIONS.md](https://github.com/feistify/secretops/blob/main/docs/OPERATIONS.md) for failure handling and daily use.

## Security model

secretops protects secret values in three layers:

1. **secretops never reveals values.** Output, plans, and audit logs carry names only, and values travel through stdin or `0600` temporary files, never command-line arguments.
2. **Operating rules for agents.** Agent instructions and policy define what an agent may run. Without privilege separation, these rules are convention.
3. **Privilege separation.** In the broker host model, a dedicated broker user holds the vault credentials and plan store, and the agent reaches secretops only through an SSH forced-command allowlist. This is the only layer that enforces the agent boundary.

Running an agent under the same user that holds vault credentials gives you a workflow gate, not a security boundary. See `docs/SECURITY_MODEL.md` for what is enforced in each deployment, the threat table, and residual risks.

## Vault configuration

Every operation that contacts Infisical needs explicit machine authentication and an explicit location. Saving a plan for a vault action needs the location (`SECRETOPS_INFISICAL_PROJECT_ID`, `SECRETOPS_INFISICAL_PATH`, and optionally `INFISICAL_DOMAIN`) but no token and no provider call. `show-plan` and GitHub-only operations need neither.

| Variable | Required | Meaning |
| --- | --- | --- |
| `INFISICAL_TOKEN` | yes | machine identity or service token; no user session is used |
| `SECRETOPS_INFISICAL_PROJECT_ID` | yes | Infisical project to read and write |
| `SECRETOPS_INFISICAL_PATH` | yes | absolute folder path inside the project, for example `/` |
| `INFISICAL_DOMAIN` | no | Infisical instance, default `https://eu.infisical.com` |
| `SECRETOPS_INFISICAL_BIN` | no | absolute path to the `infisical` executable; otherwise only `/usr/local/bin`, `/usr/bin`, and `/bin` are searched, never the caller's `PATH` |
| `SECRETOPS_PROVIDER_TIMEOUT_SECONDS` | no | timeout for each Infisical and GitHub CLI call, default 60 |

Breaking change: secretops no longer uses `infisical login` sessions, `.infisical.json` in the working directory, secret imports, personal overrides, or secret reference expansion. Secret names that the Infisical CLI reserves (such as `PATH` or `HOME`) or that secretops passes to the provider (`INFISICAL_*`, `SECRETOPS_*`, `PYTHON*`, `LD_*`) are rejected. The Infisical CLI cannot store empty or whitespace-only values, so such writes are refused before the CLI runs. Details are in `docs/design/infisical-adapter-hardening.md`.

Breaking change: plans are bound to their destination. A saved plan records `contract_version`, the vault destination (backend, canonical domain, project, path), and for GitHub actions the GitHub host (recipe field `github_host`, default `github.com`). `approve-plan` stores a digest of the operation content. Preflight and execution refuse a plan whose content changed after approval or whose vault destination differs from the current configuration, before any provider call. Plans saved by earlier versions can still be shown, but must be planned and approved again. Details are in `docs/design/plan-destination-binding.md`.

## GitHub configuration

GitHub workflows require `gh` and a stored CLI login for the executing broker user. A human configures that login; agents use only the restricted broker interface. See [GitHub authentication and migration instructions](https://github.com/feistify/secretops/blob/main/docs/BROKER_HOST.md#github-authentication-human-only).

**Breaking change:** `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, and `GITHUB_ENTERPRISE_TOKEN` are no longer forwarded, including tokens set in `broker.env`. Token-only installations must migrate to a stored broker-user login. The GitHub child process receives an explicit environment allowlist; unrelated credentials such as `INFISICAL_TOKEN` are excluded.

The executable is resolved through `/usr/local/bin:/usr/bin:/bin`, never the caller's `PATH`. For another location, set `SECRETOPS_GH_BIN` to an absolute executable path in trusted `broker.env`. Upgrade both the application and `/usr/local/bin/secretops-broker` from the same version; the wrapper clears inherited executable overrides. Keep the executable and its directory outside agent/broker-user write access.

## Current proven workflows

The current MVP has proven these workflows with dummy dev/staging smoke tests. Production execution for write, sync, generate, rotate, copy, and import-file workflows is intentionally blocked by policy until production-specific controls are expanded:

- `vault.secret.generate`
  - create-only by default
  - generates a new secret value only during approved execution
  - refuses to overwrite an existing vault secret

- `vault.secret.rotate`
  - explicit replace-existing action
  - requires the target secret to already exist
  - rotates only in the vault
  - does not automatically sync to GitHub or any target

- `vault.secret.copy_many`
  - copies explicit existing vault secret mappings between vault environments
  - proven with BACKUP_* dummy/test names from Infisical `dev` to Infisical `staging`
  - writes through the safe vault adapter temporary-file path

- `vault.secret.diff_envs`
  - compares expected secret names between two vault environments
  - governance/existence check only; does not read or print secret values
  - allowed for dev, staging, and production because it checks metadata only

- `github.env_secret.write`
  - syncs one existing vault secret to a GitHub environment secret
  - uses the GitHub CLI target adapter safely

- `github.env_secret.sync_many`
  - syncs multiple existing vault secrets to GitHub environment secrets
  - each source/target mapping is explicit
  - proven with BACKUP_* dummy/test names from Infisical `staging` to GitHub `staging`

- `github.env_secret.verify_many`
  - verifies expected GitHub environment secret names only
  - does not read or print secret values
  - replaces direct agent use of `gh secret list`
  - proven against GitHub environment `staging` in a private test repository for the BACKUP_* names

The BACKUP_* staging smoke proof used dummy/test values and these secret names only: `BACKUP_AWS_ACCESS_KEY_ID`, `BACKUP_AWS_SECRET_ACCESS_KEY`, `BACKUP_S3_BUCKET`, and `BACKUP_AWS_REGION`. `vault.secret.copy_many` copied the names from Infisical `dev` to Infisical `staging`, then `github.env_secret.sync_many` synced the same names from Infisical `staging` to GitHub environment `staging`. `execute-plan` updated 4 GitHub staging environment secrets. A follow-up `github.env_secret.verify_many` smoke proof confirmed all four expected GitHub environment secret names were present in that repository's `staging` environment, using GitHub only indirectly through `secretops execute-plan`. No secret values were read or printed during verification, and no GitHub secrets were updated. A negative production-denial smoke proof also confirmed that approved `github.env_secret.sync_many` execution for GitHub environment `production` fails safely with `FAIL production execution denied by policy: github.env_secret.sync_many`, leaves `api_call_made: False`, marks the plan `failed`, and blocks before provider calls. Production sync remains future work.

The basic read-only check action is also supported:

- `vault.read.check`

## Adding a secret you already have

`secretops add` stores one new secret whose value a human has, for example an API key created in a provider console. Direct and agent-prepared entry have passed live validation through the local sudo broker. Human submission over SSH and use inside tmux remain untested; see the [validation record](https://github.com/feistify/secretops/blob/main/docs/validation/interactive-secret-entry.md).

After [one-time client and broker setup](https://github.com/feistify/secretops/blob/main/docs/BROKER_HOST.md#human-secret-entry), choose either interactive metadata entry, a named secret, or an agent-prepared plan:

```bash
secretops add
secretops add SCALEWAY_API_KEY --env dev
secretops add --plan PLAN_ID
```

Use `--broker NAME` to select a configured broker. The first form asks for the name and environment; all three show the broker's destination before hidden value entry and confirmation. When running from a source checkout, prefix these commands with `uv run`.

Type or paste one nonblank line, at most 16 KiB in UTF-8, into the hidden prompt. Multiline values are rejected; empty/whitespace-only values and control characters are not supported. Values cannot be supplied as command arguments, environment variables, files, or raw stdin to `add`. Existing names are refused at the final check; this command does not rotate or replace them. Creating a provider API key and syncing the stored value to GitHub are separate operations.

```text
$ secretops add SCALEWAY_API_KEY --env staging
...
Value (hidden):
Save? [y/N]: y

Saved and verified.
```

The broker shows the destination and checks that the name is absent before asking for the value; the value is entered without echo, sent once over the human's own broker access (SSH or sudo), and confirmed by read-back. When an agent has prepared the plan, the human runs `secretops add --plan <plan_id>`. The agent key cannot do this step. With Infisical, create-only is a check followed by a write, not an atomic create. Setup and limits: [Human secret entry](https://github.com/feistify/secretops/blob/main/docs/BROKER_HOST.md#human-secret-entry).

## Standard safety flow

For deployed agents, use the restricted broker connection configured by a human. Replace `BROKER_ALIAS` with its SSH alias; the local recipe contains names and metadata only:

```bash
ssh BROKER_ALIAS write-recipe task.yaml < task.yaml
ssh BROKER_ALIAS plan --file task.yaml --save
ssh BROKER_ALIAS show-plan --id <plan_id>
ssh BROKER_ALIAS preflight-plan --id <plan_id>
# Wait for human approval through separate access.
ssh BROKER_ALIAS execute-plan --id <plan_id>
```

The agent cannot approve or reject plans. See [the broker operating flow](https://github.com/feistify/secretops/blob/main/docs/BROKER_HOST.md#operating-flow) for human approval commands and [agent instructions](https://github.com/feistify/secretops/blob/main/docs/AGENT_USAGE.md#instructions-for-agents-in-consumer-projects) for the consumer-project template.

The following commands are for a human operating from a development checkout with their own configured identity. They do not establish an agent security boundary:

```bash
uv run secretops plan --file examples/recipes/vault-secret-generate.yaml --save
uv run secretops show-plan --id <plan_id>
uv run secretops preflight-plan --id <plan_id>
uv run secretops approve-plan --id <plan_id>
uv run secretops execute-plan --id <plan_id>
```

The normal lifecycle is:

```text
plan --save -> show-plan -> preflight-plan -> approve-plan -> execute-plan
```

`show-plan` prints curated safe metadata, not raw JSON, including the vault destination, the GitHub host, and `plan_digest`. Approval covers the content loaded at approval time: `approve-plan` records its digest, and later content changes or a different configured vault destination stop the plan before any provider call. There is currently no `--digest` confirmation tying approval to an earlier `show-plan` view; the human must review the current plan and keep the plan store protected.

`preflight-plan` checks safe prerequisites before execution. For example:

- generate checks that the target secret is missing
- rotate checks that the target secret exists
- copy_many checks source and target vault environments before copying
- diff_envs compares expected names in two vault environments without reading values
- GitHub sync checks source secret existence without printing values
- GitHub verify_many checks configured expected names without provider calls during preflight

`execute-plan` performs the operation only through the configured adapter path.

## Dummy/dev examples

These are human-operated development-checkout examples using the configuration above. Installed deployments use the installed CLI; agents use the SSH flow above.

Generate a dummy secret in Infisical dev:

```bash
uv run secretops plan --file examples/recipes/vault-secret-generate.yaml --save
uv run secretops show-plan --id <plan_id>
uv run secretops preflight-plan --id <plan_id>
uv run secretops approve-plan --id <plan_id>
uv run secretops execute-plan --id <plan_id>
```

Rotate the same dummy secret in Infisical dev:

```bash
uv run secretops plan --file examples/recipes/vault-secret-rotate.yaml --save
uv run secretops show-plan --id <plan_id>
uv run secretops preflight-plan --id <plan_id>
uv run secretops approve-plan --id <plan_id>
uv run secretops execute-plan --id <plan_id>
```

Sync an existing dummy vault secret to GitHub environment dev:

```yaml
action: github.env_secret.sync_many
env: dev
github_host: github.com
github_owner: example-owner
github_repo: example-repo
github_environment: dev

secrets:
  - source: GENERATED_DUMMY_PASSWORD
    target: GENERATED_DUMMY_PASSWORD
```

Save the YAML above as `github-sync.yaml`, replace the example owner and repository with your intended test target, and ensure GitHub authentication is configured for the executing user (see [broker setup](https://github.com/feistify/secretops/blob/main/docs/BROKER_HOST.md#github-authentication-human-only)). Then run:

```bash
uv run secretops plan --file github-sync.yaml --save
uv run secretops show-plan --id <plan_id>
uv run secretops preflight-plan --id <plan_id>
uv run secretops approve-plan --id <plan_id>
uv run secretops execute-plan --id <plan_id>
```

## Safety invariants

These invariants are part of the product contract:

- never print secret values
- never write secret values into plan artifacts
- never write secret values into audit logs
- never pass secret values through command-line arguments
- use safe transfer paths:
  - stdin for GitHub target writes
  - temporary files with restrictive permissions for Infisical writes
- run the Infisical CLI with an explicit environment, never the broker's own, so local variables, user sessions, and working-directory config cannot affect answers
- treat a vault error as an error, never as "secret missing"
- run a saved plan only against the destination it records: the vault destination must match the configuration, GitHub calls use the planned host, and an approved plan must match its approval digest
- report an Infisical vault write as successful only after a read-back confirms it; an unconfirmed vault write is reported as an unknown outcome and never retried automatically. GitHub target writes still rely on the `gh` exit status
- planning must not generate secret values
- preflight must not generate or write secret values
- secret names may be printed when needed
- secret values must not be printed in stdout, stderr, audit logs, plan files, docs, test output, or error messages

## Current limitations and tested versions

- Infisical create-only checks existence and writes in separate calls. It refuses an existing secret when observed, but cannot guarantee atomic create-only behavior against a concurrent writer. Keep other writers coordinated; plan destination binding does not add conditional writes.
- GitHub calls have per-call timeouts and explicit acknowledged/unknown write outcomes, with partial-sync reporting. Acknowledgement relies on the CLI exit status; GitHub does not provide secret-value read-back, and name presence cannot resolve an unknown write.
- Human interactive entry is available through `secretops add`; the complete local-sudo entry-to-GitHub workflow has been validated. Human entry over SSH and use inside tmux remain untested, and the exact local terminal emulator identity/version remains unconfirmed. The separate `vault.secret.import_file` workflow remains available for human-provisioned broker-private files.
- One broker per machine, and one Infisical server, project and folder per broker. The human sets this destination; recipes cannot choose it. With one Infisical project per application, switch with `sudo secretops-configure` (open plans for the old destination then need a new plan). Several named destinations per broker are the next planned feature; see [TODO.md](https://github.com/feistify/secretops/blob/main/TODO.md).
- Tested: Infisical CLI `0.43.99`, self-hosted server `0.161.10`; GitHub CLI `2.4.0+dfsg1` for host-qualified metadata verification and a human-approved synthetic secret write with the isolated environment. A separate metadata check confirmed the test name; the value was not read back. These are tested versions, not a compatibility guarantee for every version.

See [Infisical acceptance results](https://github.com/feistify/secretops/blob/main/docs/design/infisical-adapter-hardening.md) and [destination-binding validation](https://github.com/feistify/secretops/blob/main/docs/design/plan-destination-binding.md#manual-validation-result--2026-10-18). See also [GitHub environment-isolation validation](https://github.com/feistify/secretops/blob/main/docs/validation/github-environment-isolation.md), [interactive entry validation](https://github.com/feistify/secretops/blob/main/docs/validation/interactive-secret-entry.md), and [the complete entry-to-GitHub validation](https://github.com/feistify/secretops/blob/main/docs/validation/github-target-reliability.md) for live results and their evidence limits. Installation and daily operation: [docs/INSTALL.md](https://github.com/feistify/secretops/blob/main/docs/INSTALL.md) and [docs/OPERATIONS.md](https://github.com/feistify/secretops/blob/main/docs/OPERATIONS.md), validated on a clean VM ([results and limits](https://github.com/feistify/secretops/blob/main/docs/validation/local-broker-installation.md)).

PR numbers and commit hashes in historical design and validation records refer to private development history before v0.1 and may not be accessible from this public repository.

## Current adapters

- Infisical CLI vault adapter
- GitHub CLI environment secret target adapter

secretops invokes these adapters only through narrow code paths that suppress provider stdout/stderr and avoid exposing values.

## Tests

Run the test suite with:

```bash
uv run pytest
```

The tests include safety assertions that dummy secret values never appear in CLI output, audit logs, plan artifacts, or command-line arguments.

## CI

GitHub Actions CI runs local fake-provider tests only:

```bash
uv sync --all-extras --dev
uv run pytest
sh tests/test_broker_wrappers.sh
```

CI does not require repository secrets, provider credentials, real Infisical access, or real GitHub secret access.

## Security and license

Report vulnerabilities privately; see [SECURITY.md](https://github.com/feistify/secretops/blob/main/SECURITY.md). secretops is licensed under the [Apache License 2.0](https://github.com/feistify/secretops/blob/main/LICENSE).
