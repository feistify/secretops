# Development

Working on secretops itself from a source checkout. Rules for coding agents in this repository are in [AGENTS.md](../AGENTS.md).

## Development checkout

`uv run secretops ...` in this repository reads the [configuration](CONFIGURATION.md) from the environment. These commands are for a human operating from a development checkout with their own configured identity. They do not establish an agent security boundary:

```bash
uv run secretops plan --file examples/recipes/vault-secret-generate.yaml --save
uv run secretops show-plan --id <plan_id>
uv run secretops preflight-plan --id <plan_id>
uv run secretops approve-plan --id <plan_id>
uv run secretops execute-plan --id <plan_id>
```

## Dummy/dev examples

These are human-operated development-checkout examples using the [configuration](CONFIGURATION.md). Installed deployments use the installed CLI; agents use the [broker flow](USAGE.md#plan-lifecycle).

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

Save the YAML above as `github-sync.yaml`, replace the example owner and repository with your intended test target, and ensure GitHub authentication is configured for the executing user (see [broker setup](BROKER_HOST.md#github-authentication-human-only)). Then run:

```bash
uv run secretops plan --file github-sync.yaml --save
uv run secretops show-plan --id <plan_id>
uv run secretops preflight-plan --id <plan_id>
uv run secretops approve-plan --id <plan_id>
uv run secretops execute-plan --id <plan_id>
```

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
