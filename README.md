# secretops

Let a coding agent manage your secrets without ever seeing them.

Coding agents can set up deployments, but an API key they handle can end up in their context, logs or shell history. secretops is a broker between the agent and your secrets: the agent works with secret names and plans, a human enters values and approves writes, and values move only between the vault and the target. secretops is not a vault: Infisical stays the source of truth.

![The human enters a value with secretops add, the agent saves a plan to sync it to a GitHub environment, the human approves it, and the agent executes it and verifies the secret name](https://raw.githubusercontent.com/feistify/secretops/main/docs/images/flow.png)

## How it works

1. **You enter the value.** `secretops add` asks for it without echo and stores it in your vault (Infisical).
2. **The agent prepares a plan.** For example: copy `STRIPE_KEY` from the vault to the GitHub environment `staging`. The plan holds names and destinations, never a value.
3. **You approve it.** `show-plan` shows exactly what will run; the agent cannot approve its own plans.
4. **The agent runs it** and checks the result by name. Values never appear in output, plans or logs.

The agent reaches secretops only through a restricted SSH command on your machine. A separate broker account holds the vault token and the plans, so the agent cannot read them.

## What it can do

- Store a secret you already have (hidden entry), or generate, rotate, import and copy secrets between environments in Infisical
- Sync secrets from Infisical to GitHub environment secrets and verify they are there
- Keep an audit log without values
- Refuse production writes by default, even with approval

## Example

```sh
# You: store the value
secretops add STRIPE_KEY --env staging

# Agent: write a recipe and plan the sync
cat > sync.yaml <<'YAML'
action: github.env_secret.sync_many
env: staging
github_owner: example-owner
github_repo: example-app
github_environment: staging
secrets:
  - source: STRIPE_KEY
    target: STRIPE_KEY
YAML
secretops-local write-recipe sync.yaml < sync.yaml
secretops-local plan --file sync.yaml --save          # prints plan_id

# You: review and approve
sudo -u secretops-broker /usr/local/bin/secretops-broker show-plan --id PLAN_ID
sudo -u secretops-broker /usr/local/bin/secretops-broker approve-plan --id PLAN_ID

# Agent: run it
secretops-local execute-plan --id PLAN_ID             # OK GitHub writes acknowledged: 1
```

The complete walkthrough, with verification and cleanup, is in the [demo](https://github.com/feistify/secretops/blob/main/docs/DEMO.md).

## Get started

secretops installs as a local broker on Linux, from this repository: `pip install secretops` alone does not set up the broker account and the restricted agent access.

1. [Install](https://github.com/feistify/secretops/blob/main/docs/INSTALL.md) the broker and connect Infisical and GitHub.
2. Try the [demo](https://github.com/feistify/secretops/blob/main/docs/DEMO.md) with a test project and made-up values.
3. Add the [agent instructions](https://github.com/feistify/secretops/blob/main/docs/AGENT_USAGE.md#instructions-for-agents-in-consumer-projects) to your project, so your agent uses the broker.

Working with a coding agent? Point it to [docs/AGENT_START.md](https://github.com/feistify/secretops/blob/main/docs/AGENT_START.md): it can check prerequisites and prepare the installation commands, while you run the steps that need `sudo`, a token or a login.

## Status and limits

**v0.1.2, early-stage software.** Try it with test values first.

- The installer supports Ubuntu 24.04 and needs Python 3.11 or newer.
- Run the agent under its own account: under the same user, secretops is a workflow gate, not a security boundary.
- One vault: Infisical. One target: GitHub environment secrets.
- One Infisical project and folder per broker; switch with `sudo secretops-configure`.
- GitHub cannot return stored values, so secretops verifies names, not values.

Details: [limitations and tested versions](https://github.com/feistify/secretops/blob/main/docs/LIMITATIONS.md).

## Documentation

| Read this | For |
| --- | --- |
| [INSTALL](https://github.com/feistify/secretops/blob/main/docs/INSTALL.md) | Installing a local broker |
| [OPERATIONS](https://github.com/feistify/secretops/blob/main/docs/OPERATIONS.md) | Daily use, failures, token renewal, upgrade, backup |
| [USAGE](https://github.com/feistify/secretops/blob/main/docs/USAGE.md) | Adding secrets, the plan lifecycle, all actions |
| [AGENT_START](https://github.com/feistify/secretops/blob/main/docs/AGENT_START.md) | Start page for coding agents |
| [AGENT_USAGE](https://github.com/feistify/secretops/blob/main/docs/AGENT_USAGE.md) | How agents work with the broker; instructions for your project |
| [SECURITY_MODEL](https://github.com/feistify/secretops/blob/main/docs/SECURITY_MODEL.md) | What is enforced, threats, safety rules |
| [CONFIGURATION](https://github.com/feistify/secretops/blob/main/docs/CONFIGURATION.md) | Vault and GitHub settings |
| [BROKER_HOST](https://github.com/feistify/secretops/blob/main/docs/BROKER_HOST.md) | Deployment variants: remote or temporary broker |
| [DEVELOPMENT](https://github.com/feistify/secretops/blob/main/docs/DEVELOPMENT.md) | Working on secretops itself |

## Security and license

Report vulnerabilities privately; see [SECURITY.md](https://github.com/feistify/secretops/blob/main/SECURITY.md). secretops is licensed under the [Apache License 2.0](https://github.com/feistify/secretops/blob/main/LICENSE).
