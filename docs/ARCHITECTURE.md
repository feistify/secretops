# secretops architecture

secretops is a CLI-first SecretOps broker. The CLI runs on the broker host; agents can reach it locally or remotely through an SSH forced-command wrapper. It coordinates safe secret lifecycle operations without becoming a vault and without exposing secret values.

The vault is the source of truth. Targets receive values only through explicit approved operations.

## High-level flow

```text
recipe -> policy -> plan artifact -> show/preflight -> approval -> execution -> audit
                                      |                         |
                                      v                         v
                                vault adapter              target adapter
```

## Core concepts

### Recipe

A recipe is a YAML request for a secret operation. It names the action, environment, secret names, target metadata, and safe generator metadata when needed.

Recipes must not contain secret values.

Example action fields:

- `action`
- `env`
- `source_env`
- `target_env`
- `secret_name`
- `generator`
- `create_mode`
- `rotation_mode`
- GitHub target metadata
- explicit source/target secret mappings

### Plan artifact

A saved plan artifact is the durable metadata record for an intended operation.

Plan artifacts include safe metadata such as:

- plan ID
- status
- action
- environment
- source secret name
- target metadata
- generator metadata
- `contract_version`
- `vault_destination` for actions that use the vault
- `target.target_host` for GitHub actions
- `approval_digest` when a human approves the plan
- `secret_value_included: false`

Plan artifacts must never include raw secret values.

Saved plan files under `.plans/*.json` are local JSON state and are mutable by any local actor with filesystem access. They are workflow artifacts, not a strong trust boundary. Loading a supported plan enforces a strict per-action schema. Before provider calls, preflight and execution check the approval digest when present or required by approved status, and compare the recorded vault destination with current configuration. Execution also enforces action policy, environments, and required approval status. The digest detects changes only while its stored value remains intact; it does not replace plan-store ownership.

Secret names are intentionally restricted to env-var-style identifiers matching `^[A-Z][A-Z0-9_]*$`. This rejects lowercase names, spaces, shell metacharacters, quotes, slashes, hyphens, equals signs, and empty names so names can be safely passed through vault and target adapter boundaries without becoming code or shell syntax.

### Policy

Policy controls which actions are allowed and which production-style operations must be denied or approved.

The current model keeps the existing allow/deny action lists and adds per-action metadata:

- `requires_approval`
- `allowed_environments`
- `production_allowed`

Production execution is currently allowed only for metadata-only checks `vault.read.check`, `vault.secret.diff_envs`, and `github.env_secret.verify_many`. Write, sync, generate, add, rotate, copy, and import-file production workflows are intentionally blocked until production-specific policy controls are expanded. Current write/sync proven workflows are dev/staging only.

### Approval

Write, rotation, and sync operations are expected to run from saved approved plans. Approval is a workflow gate for the local SecretOps lifecycle; it is not cryptographic proof of human intent and is not a strong security boundary by itself.

An approval covers one operation on one destination. A saved plan records its `contract_version`, its vault destination (backend, canonical domain, project, path), and for GitHub actions the GitHub host. `approve-plan` stores `approval_digest`, a versioned SHA-256 digest of the plan content excluding `status` and `approval_digest`. Each of `approve-plan`, `preflight-plan`, and `execute-plan` reads the plan file once and checks it against a strict schema; preflight and execution then verify the digest and compare the recorded vault destination with the configuration before any provider call, and the vault adapter is bound to the recorded destination for the whole operation. See [design/plan-destination-binding.md](design/plan-destination-binding.md).

`vault.secret.add` is the exception to `approve-plan`: its value is entered by a human, so approval and execution happen together in `secretops add`. The broker's human endpoints record the approval with the digest the human reviewed, under a per-plan lock; `approve-plan` and `execute-plan` refuse the action. See [design/interactive-secret-entry.md](design/interactive-secret-entry.md).

The SSH broker deployment enforces a separate human approval path: the agent key cannot approve or reject plans, and the broker's plan store is not writable by the agent. Local execution under an account that can edit plan files does not provide that boundary. Plan-file signatures are not implemented. See [BROKER_HOST.md](BROKER_HOST.md).

Typical status transitions:

```text
planned -> approved -> executed
planned -> executed  (actions that do not require approval)
planned -> rejected
planned -> failed
approved -> failed
```

These are illustrative transitions, not a one-time-execution guarantee. Preflight never changes plan status. Invalid JSON, schema violations, and missing or unsupported contract versions leave the plan file untouched; no new status is written. A valid supported plan with a digest or destination mismatch is marked `failed` by execution.

### Vault adapter

The vault adapter is responsible for safe interaction with the source-of-truth vault.

Current vault adapter:

- Infisical CLI vault adapter

Current safety behavior:

- existence checks do not print values
- reads capture values only for handoff to execution targets
- writes use temporary files with restrictive permissions
- provider stdout/stderr are captured and not printed
- the CLI runs with an explicit environment and explicit project, path, and read flags
- existence and reads distinguish present, empty, missing, and error; errors stop the operation
- writes are confirmed by read-back; unconfirmed writes are reported as unknown outcomes
- create-only requests (`create_secret`) return confirmed, exists, or unknown, and the adapter declares whether the refusal of an existing secret is atomic; the Infisical CLI adapter is not (its `secrets set` is an upsert)

See `docs/design/infisical-adapter-hardening.md` for the full contract.

### Target adapter

Target adapters write existing vault values into destination systems.

Current target adapter:

- GitHub CLI environment secret target

Current safety behavior:

- values are passed through stdin
- values are not passed in command-line arguments
- provider stdout/stderr are captured and not printed
- each `gh` call is bounded by `SECRETOPS_PROVIDER_TIMEOUT_SECONDS`; on timeout `gh` is killed and never rerun
- launching `gh` and communicating with it are separate steps: a launch failure means nothing was sent, while anything after launch other than exit status 0 is an unknown write
- writes are "acknowledged", not confirmed: GitHub secret values cannot be read back, and a later metadata check cannot resolve an unknown write
- multi-secret sync stops at the first write that is not acknowledged and reports every mapping as acknowledged, unknown, not written, or not attempted
- metadata listings accept any valid GitHub secret name, compare only the expected names, and never print others; a malformed listing is an error, not an empty result

### Audit

Audit events record safe operation metadata and status.

Audit logs may include:

- action
- secret name
- environment
- plan ID
- status

Audit logs must never include secret values.

## Current actions

### `vault.read.check`

Checks whether a named secret exists in a vault environment.

- does not read or print the value
- safe read-only workflow

### `vault.secret.generate`

Creates a new vault secret.

Semantics:

- create-only by default
- current mode: `create_mode: create_only`
- execution fails safely if the target secret already exists
- generation happens only during approved execution
- preflight checks that the target secret is missing
- values are written through the safe vault adapter path

### `vault.secret.rotate`

Rotates an existing vault secret.

Semantics:

- explicit replace-existing workflow
- current mode: `rotation_mode: replace_existing`
- execution fails safely if the target secret is missing
- generation happens only during approved execution
- preflight checks that the target secret exists
- rotation only updates the vault
- target sync remains a separate explicit plan

### `vault.secret.import_file`

Imports an existing credential file provisioned by a human in the broker domain.

- the file must have no group or other permissions; mode `0600` is recommended
- planning parses the file locally to record names, without contacting the vault or printing values
- execution checks the expected names and requires a saved approved plan
- only `create_mode: create_only` is supported; importing over existing secrets is refused
- confirmed, unknown, and partial write results use the vault adapter contract
- the agent never reads or transports the source file

The parser accepts UTF-8 `NAME=value` lines, blank lines, full-line comments, and an optional `export ` prefix. It strips surrounding whitespace and one matching pair of quotes; it does not evaluate shell syntax, expand references, decode escapes, or support multi-line values. Duplicate or invalid names are rejected. Empty values such as `NAME=` are parsed, but Infisical writes of empty or whitespace-only values are refused.

### `vault.secret.copy_many`

Copies explicit existing secret mappings from one vault environment to another vault environment.

Semantics:

- source and target environments are explicit
- each source/target secret-name mapping is listed in the recipe
- planning stores names and metadata only
- preflight checks source existence and target existence
- `create_mode: create_only` fails safely if any target secret already exists
- source values are read only during approved execution
- target values are written through the safe vault adapter temporary-file path

### `vault.secret.diff_envs`

Compares expected secret names between two vault environments.

Semantics:

- source and target environments are explicit
- each expected secret name is listed in the recipe as `name`
- planning stores environment names and expected secret names only
- preflight checks whether each expected name exists in source and target environments
- execution performs the same safe existence comparison and updates plan status
- approval is not required because this is a metadata/existence check only
- production is allowed because secret values are never read or printed

### `vault.secret.add`

Creates one new vault secret whose value a human enters.

Semantics:

- planned by an agent (recipe with `secret_name` and `env`) or by `secretops add` itself; the plan records `create_mode: create_only` and `value_source: human_prompt`, never the value
- preflight checks that the secret is absent
- the human runs `secretops add` (or `secretops add --plan <plan_id>`) through human broker access: the broker reviews the plan, the human enters the value in a hidden prompt and confirms, and the broker approves and executes with the reviewed digest
- `approve-plan` and `execute-plan` refuse the action; `reject-plan` works
- the write is confirmed by read-back; an unconfirmed write is an unknown outcome and is never retried
- with Infisical, create-only is check-then-write, not atomic
- production is denied by the default policy

### `github.env_secret.write`

Writes one existing vault secret to a GitHub environment secret.

Semantics:

- source value comes from the vault
- destination is a GitHub environment secret
- requires explicit target metadata
- value is sent to the GitHub CLI through stdin

### `github.env_secret.sync_many`

Distributes existing vault secrets to GitHub environment secrets using explicit mappings.

Semantics:

- each source/target mapping is listed in the recipe
- preflight checks that mapped source secrets exist
- execution writes mapped values to GitHub environment secrets
- no automatic generation or rotation happens during sync

### `github.env_secret.verify_many`

Verifies that expected GitHub environment secret names exist.

Semantics:

- each expected GitHub environment secret name is listed in the recipe
- planning stores names and target metadata only
- preflight confirms verification metadata is configured and does not call GitHub
- execution lists GitHub environment secret metadata through the secretops adapter path
- execution compares expected names with returned names
- secret values are never read or printed
- provider raw output is captured and not printed
- this replaces direct agent use of `gh secret list`

## Action semantics summary

```text
generate    = create-only vault secret creation
add         = create-only vault secret creation with a human-entered value
rotate      = explicit replace-existing vault secret rotation
copy_many   = copy explicit existing secret mappings between vault environments
diff_envs   = compare expected secret names between vault environments
sync_many   = distribute existing secrets to target systems
verify_many = verify expected target secret names exist without reading values
```

These are intentionally separate workflows so agents cannot accidentally rotate, overwrite, or distribute secrets without an explicit plan.

## Smoke proofs

### Dummy/dev lifecycle

The completed dummy/dev lifecycle proof demonstrated:

- generated `GENERATED_DUMMY_PASSWORD` into Infisical dev
- enforced create-only behavior when the generated secret already existed
- rotated `GENERATED_DUMMY_PASSWORD` in Infisical dev
- synced `GENERATED_DUMMY_PASSWORD` to GitHub environment dev
- kept GitHub sync as a separate explicit plan after rotation
- printed no secret value
- wrote no secret value into plan artifacts
- wrote no secret value into audit logs
- avoided passing values through command-line arguments

Only secret names and safe metadata were displayed.

### BACKUP_* dummy/test staging workflow

A real safe smoke proof demonstrated the intended staging path for backup-related dummy/test secret names:

```text
Infisical dev -> Infisical staging -> GitHub staging -> safe metadata verification
```

- vault copy source: Infisical `dev`
- vault copy target: Infisical `staging`
- vault copy action: `vault.secret.copy_many`
- GitHub sync source: Infisical `staging`
- GitHub sync target: GitHub environment `staging`
- GitHub sync action: `github.env_secret.sync_many`
- GitHub verification action: `github.env_secret.verify_many`
- secret names:
  - `BACKUP_AWS_ACCESS_KEY_ID`
  - `BACKUP_AWS_SECRET_ACCESS_KEY`
  - `BACKUP_S3_BUCKET`
  - `BACKUP_AWS_REGION`
- `preflight-plan` confirmed all four Infisical `dev` source secrets existed and all Infisical `staging` copy targets were missing before copy
- `execute-plan` copied 4 vault secrets from Infisical `dev` to Infisical `staging`
- `preflight-plan` confirmed all four Infisical `staging` source secrets existed before GitHub sync
- `execute-plan` updated 4 GitHub staging environment secrets
- GitHub secrets were not updated directly; writes went through `secretops execute-plan`
- no secret values were printed

A follow-up metadata verification smoke proof demonstrated that `github.env_secret.verify_many` can safely confirm GitHub environment secret names without direct agent use of `gh secret list`:

- target: GitHub environment `staging` in a private test repository
- expected names:
  - `BACKUP_AWS_ACCESS_KEY_ID`
  - `BACKUP_AWS_SECRET_ACCESS_KEY`
  - `BACKUP_S3_BUCKET`
  - `BACKUP_AWS_REGION`
- result: all four expected names were present
- GitHub was used only indirectly through `secretops execute-plan`
- no GitHub secrets were updated during verification
- no secret values were read or printed

No raw secret values were written into plan artifacts, audit logs, or docs. Production sync remains future work and should require stronger policy controls.

### Production-denial safety proof

A real negative smoke proof demonstrated that production GitHub secret sync is blocked by policy before provider calls:

- action: `github.env_secret.sync_many`
- source environment: `production`
- target GitHub environment: `production`
- plan ID: `7f5543679b624a40bc993277c9038028`
- the plan was created and approved through the saved-plan lifecycle
- `execute-plan` failed safely with `FAIL production execution denied by policy: github.env_secret.sync_many`
- final plan status became `failed`
- `show-plan` metadata remained `api_call_made: False`
- no Infisical, GitHub, or provider command was run directly by the agent
- no secret values were printed

This proves production sync is denied before Infisical or GitHub provider calls can occur.

## Boundaries

Current boundaries:

- CLI execution on the broker host, with local or remote agent access through the SSH forced-command wrapper
- no HTTP API, MCP server, or persistent broker daemon
- Infisical CLI is the only vault adapter
- GitHub CLI environment secret target is the only target adapter
- no Jenkins adapter yet
- no automatic combined workflow yet
- rotation does not automatically sync to targets
- production writes are blocked by the current policy even with approval; permitted metadata-only production checks are separate

Future work should preserve the same safety invariants when new adapters or remote boundaries are added.
