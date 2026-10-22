# AGENTS.md

**Installing secretops or using it for another project?** Start at [docs/AGENT_START.md](docs/AGENT_START.md). The rest of this file is for developing secretops itself in this repository.

## Purpose

secretops is a CLI-first SecretOps broker. Its purpose is to help agents and developers plan and execute safe secret operations without exposing secret values.

The vault is the source of truth. secretops operates on secret names, actions, environments, recipes, saved plans, policy checks, approvals, audit events, and safe status messages — never raw secret values.

secretops is not a vault. It uses external vaults and targets only through narrow adapters.

## Allowed commands for agents

These commands are for development in this repository. For deployed agent usage, use the restricted SSH broker described in [docs/AGENT_USAGE.md](docs/AGENT_USAGE.md), including its consumer-project instruction template. The human supplies the broker connection; agents must not discover credentials or configure their own approval access.

Agents may run only these project commands unless the user explicitly approves a broader command:

```bash
uv run secretops version
uv run secretops actions
uv run secretops plan-vault-read --name DUMMY_SECRET --env dev
uv run secretops check-vault-read --name DUMMY_SECRET --env dev
uv run secretops plan-recipe --file examples/recipes/dummy-vault-check.yaml
uv run secretops execute-recipe --file examples/recipes/dummy-vault-check.yaml
uv run secretops plan --file examples/recipes/dummy-vault-check.yaml --save
uv run secretops plan --file examples/recipes/vault-secret-generate.yaml --save
uv run secretops plan --file examples/recipes/vault-secret-rotate.yaml --save
uv run secretops show-plan --id <plan_id>
uv run secretops preflight-plan --id <plan_id>
uv run secretops execute-plan --id <plan_id>
uv run pytest
```

Agents may use normal repository inspection/editing tools to read and modify non-secret project source, tests, docs, and example recipe files.

Agents may create temporary recipe files that contain secret names and target metadata only. Temporary recipes must never contain secret values.

## Forbidden commands for agents

Agents must never run commands that print, list, dump, or bulk-export secrets.

Forbidden examples:

```bash
infisical secrets
infisical secrets get
printenv
env
gh secret list
```

Agents must also never read or print:

- `.env` files
- tokens
- keys
- credentials
- private files
- machine identity secrets
- any provider secret listing or bulk export

## No-secret-output rule

Secret values must never appear in:

- stdout
- stderr
- audit logs
- plan artifacts
- test output
- documentation examples
- error messages
- debug logs
- command-line arguments

Secret names may be printed when needed. Secret values must not be printed.

The dummy test secret value is intentionally used only in tests; tests must ensure it never appears in stdout, stderr, audit logs, or plan artifacts.

## Plan-before-execute rule

Agents should prefer saved planning commands before execution commands.

Planning commands describe the intended action, secret name, environment, policy decision, target metadata, and safety expectations. Planning commands must not execute vault reads, generate secret values, write target values, or print secret values.

Standard lifecycle:

```bash
uv run secretops plan --file <recipe.yaml> --save
uv run secretops show-plan --id <plan_id>
uv run secretops preflight-plan --id <plan_id>
# Human only, through separate broker access: approve-plan --id <plan_id>
uv run secretops execute-plan --id <plan_id>
```

Do not execute a plan when preflight reports a safety failure. Report the failure to the human.

Approval and rejection belong to the human, not the agent. Ask the human to review and approve a write plan through their separate broker access. Do not attempt execution while approval is pending. The restricted agent SSH wrapper denies both `approve-plan` and `reject-plan`.

## Production restrictions

The default policy blocks production generation, rotation, import, copy, and GitHub writes/sync even when a human approves the plan. Approval does not override environment policy. Metadata-only checks can run in production where the action policy permits them.

`production.execute_without_approval` is also explicitly denied. Agents must not bypass policy checks or change policy to enable an operation.

## Current supported safe workflows

Current actions:

```text
vault.read.check
vault.secret.generate
vault.secret.rotate
vault.secret.import_file
vault.secret.copy_many
vault.secret.diff_envs
vault.secret.add
github.env_secret.write
github.env_secret.sync_many
github.env_secret.verify_many
```

Current semantics:

- `vault.read.check` checks whether a named secret exists without printing the value.
- `vault.secret.generate` is create-only by default and must not overwrite existing secrets.
- `vault.secret.rotate` is the explicit replace-existing vault action and requires the target secret to exist.
- `vault.secret.import_file` imports a human-provisioned, broker-readable file; the agent must not read or transport its contents.
- `vault.secret.copy_many` copies explicit mappings between vault environments.
- `vault.secret.diff_envs` checks expected names across environments without reading values.
- `vault.secret.add` creates one new secret whose value a human enters. Agents may plan, show, and preflight it; the human completes it with `secretops add --plan <plan_id>` through their own broker access. `approve-plan` and `execute-plan` refuse it. Never ask the human to send the value to you.
- `github.env_secret.write` syncs one existing vault secret to GitHub environment secrets.
- `github.env_secret.sync_many` syncs explicit source/target mappings for existing vault secrets.
- `github.env_secret.verify_many` checks expected target names through the adapter without reading values.

This list describes capabilities, not permission to bypass the command allowlist or deployment policy. `secretops actions` reports the current policy's allowed and denied actions; it is not a complete command or recipe schema.

Rotation only updates the vault. GitHub or other target sync remains a separate explicit saved plan.

## Adapter scope

Current implementation uses:

- Infisical CLI vault adapter
- GitHub CLI environment secret target adapter

Do not add other vault or external-service integrations unless explicitly requested.

Do not call provider CLIs directly except through the approved `uv run secretops ...` surfaces requested by the user.
