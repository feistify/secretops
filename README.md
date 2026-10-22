# secretops

Let a coding agent manage your secrets without ever seeing them.

Coding agents can set up deployments, but an API key they handle can end up in their context, logs or shell history. secretops is a broker between the agent and your secrets: the agent works with secret names and plans, a human enters values and approves writes, and values move only between the vault and the target. secretops is not a vault: Infisical stays the source of truth.

![The human enters a value with secretops add, the agent saves a plan to sync it to a GitHub environment, the human approves it, and the agent executes it and verifies the secret name](https://raw.githubusercontent.com/feistify/secretops/main/docs/images/flow.png)

## How it works

1. **You enter the value.** `secretops add` asks for it without echo and stores it in your vault (Infisical).
2. **The agent prepares a plan.** For example: copy `STRIPE_KEY` from the vault to the GitHub environment `staging`. The plan holds names and destinations, never a value.
3. **You approve it.** `show-plan` shows exactly what will run; the agent cannot approve its own plans.
4. **The agent runs it** and checks the result by name. Values never appear in output, plans or logs.


## The pieces

```text
you    ── secretops add, approve-plan ──────────────┐
                                                    ▼
agent  ── secretops-local (restricted SSH) ──▶  broker  ◀──▶  Vault (Infisical): values
                                                    └──────▶  GitHub environment secrets
```

- **Broker:** a separate Linux account on your machine (`secretops-broker`). It holds the Infisical token, the GitHub login and the saved plans, and it is the only place secretops runs with access to values.
- **Agent access:** the agent talks to the broker only through `secretops-local`, an SSH command limited to a fixed list: plan, show, check and run approved plans. It cannot approve, open a shell or read the token.
- **You:** enter values and approve plans from your own terminal.

The boundary is only as strong as the separation: recommended are three accounts on one machine (you, the agent, the broker); strongest is a broker on another machine. If the agent runs as your own user, it can use your `sudo` and secretops is a workflow gate, not a boundary. See [deployment levels](https://github.com/feistify/secretops/blob/main/docs/SECURITY_MODEL.md#deployment-levels).

**Why not just `pip install secretops`?** The PyPI package is only the program. The separation above (broker account, restricted agent access, your tools) needs root and is set up by the installer from the GitHub release, which takes the program from PyPI.

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

On Ubuntu 24.04 or Debian 12, with `curl`, `python3-venv`, `openssh-server` and a root-owned `infisical` CLI installed, in your own terminal:

```sh
curl -fsSLO https://github.com/feistify/secretops/releases/download/v0.1.3/secretops-0.1.3-installer.tar.gz
sha256sum secretops-0.1.3-installer.tar.gz      # compare with the release notes
tar -xzf secretops-0.1.3-installer.tar.gz && cd secretops-0.1.3-installer
sudo ./install.sh --create-agent-user agent --infisical-domain https://app.infisical.com
sudo secretops-configure        # Infisical project, folder and token (hidden prompt)
```

`--create-agent-user` creates a separate account without sudo for your coding agent; to use an existing account, give `--agent-user NAME` instead. The installer sets up the broker account, the restricted agent access and your tools, and installs secretops 0.1.3 from PyPI, checked against the hash in the bundle. `sudo secretops-uninstall` removes it again.

1. Follow the [installation guide](https://github.com/feistify/secretops/blob/main/docs/INSTALL.md) for the details: prerequisites, a separate agent account, the GitHub login and checks.
2. Try the [demo](https://github.com/feistify/secretops/blob/main/docs/DEMO.md) with a test project and made-up values.
3. Add the [agent instructions](https://github.com/feistify/secretops/blob/main/docs/AGENT_USAGE.md#instructions-for-agents-in-consumer-projects) to your project, so your agent uses the broker.

Working with a coding agent? Point it to [docs/AGENT_START.md](https://github.com/feistify/secretops/blob/main/docs/AGENT_START.md): it can check prerequisites and prepare the installation commands, while you run the steps that need `sudo`, a token or a login.

## Status and limits

**v0.1.3, early-stage software.** Try it with test values first.

| Available now | Current limits |
| --- | --- |
| Hidden entry, generation, rotation, import and environment copy in Infisical | One Infisical project and folder per broker; switch with `sudo secretops-configure` |
| Sync to GitHub environment secrets and name verification | GitHub cannot return values: secretops verifies names, not values |
| Saved plans bound to their destination, human approval, audit without values | Production writes are refused by the default policy |
| Local broker with restricted agent access, an installer and an uninstaller | Ubuntu 24.04 or Debian 12 with Python 3.11 or newer; run the agent under its own account, otherwise it is a workflow gate, not a security boundary |

Details: [limitations and tested versions](https://github.com/feistify/secretops/blob/main/docs/LIMITATIONS.md).

## Documentation

| Read this | For |
| --- | --- |
| [INSTALL](https://github.com/feistify/secretops/blob/main/docs/INSTALL.md) | Installing a local broker |
| [OPERATIONS](https://github.com/feistify/secretops/blob/main/docs/OPERATIONS.md) | Daily use, failures, token renewal, upgrade, backup |
| [USAGE](https://github.com/feistify/secretops/blob/main/docs/USAGE.md) | Adding secrets, the plan lifecycle, all actions |
| [RECIPES](https://github.com/feistify/secretops/blob/main/docs/RECIPES.md) | Every action: fields, approval, environments, example |
| [AGENT_START](https://github.com/feistify/secretops/blob/main/docs/AGENT_START.md) | Start page for coding agents |
| [AGENT_USAGE](https://github.com/feistify/secretops/blob/main/docs/AGENT_USAGE.md) | How agents work with the broker; instructions for your project |
| [SECURITY_MODEL](https://github.com/feistify/secretops/blob/main/docs/SECURITY_MODEL.md) | What is enforced, threats, safety rules |
| [CONFIGURATION](https://github.com/feistify/secretops/blob/main/docs/CONFIGURATION.md) | Vault and GitHub settings |
| [BROKER_HOST](https://github.com/feistify/secretops/blob/main/docs/BROKER_HOST.md) | Deployment variants: remote or temporary broker |
| [DEVELOPMENT](https://github.com/feistify/secretops/blob/main/docs/DEVELOPMENT.md) | Working on secretops itself |

## Security and license

Report vulnerabilities privately; see [SECURITY.md](https://github.com/feistify/secretops/blob/main/SECURITY.md). secretops is licensed under the [Apache License 2.0](https://github.com/feistify/secretops/blob/main/LICENSE).
