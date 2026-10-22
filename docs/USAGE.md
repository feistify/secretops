# Use secretops

How a human adds secrets, how an agent runs the plan lifecycle through the broker, and what each action does. Installation is in [INSTALL.md](INSTALL.md), daily operation of a local broker in [OPERATIONS.md](OPERATIONS.md).

## Adding a secret you already have

`secretops add` stores one new secret whose value a human has, for example an API key created in a provider console. Direct and agent-prepared entry have passed live validation through the local sudo broker. Human submission over SSH and use inside tmux remain untested; see the [validation record](validation/interactive-secret-entry.md).

After [one-time client and broker setup](BROKER_HOST.md#human-secret-entry), choose either interactive metadata entry, a named secret, or an agent-prepared plan:

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

The broker shows the destination and checks that the name is absent before asking for the value; the value is entered without echo, sent once over the human's own broker access (SSH or sudo), and confirmed by read-back. When an agent has prepared the plan, the human runs `secretops add --plan <plan_id>`. The agent key cannot do this step. With Infisical, create-only is a check followed by a write, not an atomic create. Setup and limits: [Human secret entry](BROKER_HOST.md#human-secret-entry).

## Plan lifecycle

For deployed agents, use the restricted broker connection configured by a human. Replace `BROKER_ALIAS` with its SSH alias; the local recipe contains names and metadata only:

```bash
ssh BROKER_ALIAS write-recipe task.yaml < task.yaml
ssh BROKER_ALIAS plan --file task.yaml --save
ssh BROKER_ALIAS show-plan --id <plan_id>
ssh BROKER_ALIAS preflight-plan --id <plan_id>
# Wait for human approval through separate access.
ssh BROKER_ALIAS execute-plan --id <plan_id>
```

The agent cannot approve or reject plans. See [the broker operating flow](BROKER_HOST.md#operating-flow) for human approval commands and [agent instructions](AGENT_USAGE.md#instructions-for-agents-in-consumer-projects) for the consumer-project template.

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

## Actions

Fields and an example for every action are in the [recipe reference](RECIPES.md).

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
