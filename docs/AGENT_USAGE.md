# Agent usage model

An agent starting from scratch should read [AGENT_START.md](AGENT_START.md) first. This page explains the deployment model behind it and holds the instructions template for consumer projects.

secretops is intended to be used by agents as an installed CLI package, not as a source checkout placed in the agent workspace.

Agents should work with secret names, environments, recipes, saved plans, policy decisions, approval status, audit events, and safe status output. Agents must not receive or print raw secret values.

## Installation

A human installs the broker as described in [INSTALL.md](INSTALL.md); an agent can help as described in [AGENT_START.md](AGENT_START.md). The agent gets only `secretops-local`, never the secretops source, a Python installation of it, or its credentials. Other deployment shapes (a broker on another machine, an ephemeral broker) are in [BROKER_HOST.md](BROKER_HOST.md).

## Agent operating model

Agents may run saved-plan and safe metadata commands such as:

```bash
secretops version
secretops actions
secretops plan --file <recipe.yaml> --save
secretops show-plan --id <plan_id>
secretops preflight-plan --id <plan_id>
secretops execute-plan --id <plan_id>
```

`secretops actions` lists the current policy's allowed and denied actions so agents can self-orient without touching provider CLIs. It does not describe recipe schemas or guarantee that a particular operation will pass environment, approval, or preflight checks.

Under the broker host model (`BROKER_HOST.md`), the agent also uploads recipe YAML through the wrapper's `write-recipe <name>.yaml` verb (stdin), because the restricted SSH key disables scp. Recipes are then referenced by bare name: `secretops plan --file <name>.yaml --save`. Recipes carry secret names and metadata only, never values.

Agents may also run safe read, diff, or verify commands when those commands are available and explicitly approved for the deployment. These commands must report only safe metadata such as names, environments, presence/missing status, policy decisions, and plan status. They must not reveal secret values.

Agents must not run:

```bash
secretops approve-plan
infisical
infisical secrets
gh secret
env
printenv
```

Agents must also not run provider CLIs for secrets, commands that read `.env` files, or commands that read token, key, credential, private, or machine identity files.

Human approval remains outside the agent permission set. An agent can prepare, show, preflight, and execute an already approved plan when policy allows it, but the approval action itself belongs to a human or a separate trusted approval workflow.

When a secret's value exists only with the human (for example a key created in a provider console), the agent prepares a `vault.secret.add` plan (`action`, `secret_name`, `env`; optional `create_mode: create_only` and `value_source: human_prompt`) and asks the human to run `secretops add --plan <plan_id>` on their own computer. The human reviews the destination, enters the value in a hidden prompt, and confirms; that confirmation is the approval. The agent then follows the result with `show-plan` (`executed`, or `failed` with a reason) and prepares any target sync as a separate plan. `approve-plan` and `execute-plan` refuse this action. The agent never asks for the value, and the human never pastes it into the agent's session.

## Deployment environment pinning

Every agent deployment must pin:

- `SECRETOPS_PLAN_DIR`
- `SECRETOPS_AUDIT_PATH`
- `SECRETOPS_POLICY_PATH`
- `INFISICAL_DOMAIN` (when targeting a self-hosted vault)
- `INFISICAL_TOKEN`, `SECRETOPS_INFISICAL_PROJECT_ID`, and `SECRETOPS_INFISICAL_PATH` in the broker environment, never in the agent's
- `SECRETOPS_INFISICAL_BIN`, pointing to a CLI the agent cannot modify

The default plan and audit paths are cwd-relative (`.plans` and `.audit/secretops.jsonl`), which fragments state when the CLI runs from arbitrary directories. The default policy is a packaged resource, not a cwd-relative file. Pin an explicit protected policy path for a managed broker so policy selection is deliberate and humans and agents use the same deployment settings.

`SECRETOPS_PLAN_DIR` must live outside agent-writable workspaces: plan status is ordinary JSON, and an agent-writable plan directory would allow status tampering that bypasses the approval gate. The broker host layout in `BROKER_HOST.md` satisfies all of this by construction.

## Source exposure and isolation limits

Python wheels reduce repository and source-tree exposure for agents because the agent no longer needs a source checkout. Wheels are still not a cryptographic code-hiding boundary.

Stronger isolation requires additional controls such as:

- a narrow wrapper around `secretops`
- a separate Unix user
- a container with restricted mounts and environment
- a future MCP or daemon model that exposes only safe broker operations

The wrapper and separate Unix user are implemented by the broker host model: see `BROKER_HOST.md` and the wrapper scripts in `scripts/`. Container isolation is an optional deployment choice, not something the wrapper scripts provision.

## Instructions for agents in consumer projects

Agents need both operating instructions and a human-provisioned broker connection. The repository's own `AGENTS.md` governs development of secretops; it does not automatically configure agents working in other repositories.

With a local broker installed by `scripts/install.sh`, the connection is `secretops-local`: replace `ssh BROKER_ALIAS` with it below. Otherwise, the human configuring the deployment should configure an SSH alias with the broker host, port, restricted agent identity, and verified host key. Before copying the following section into the consumer project's `AGENTS.md`, replace `BROKER_ALIAS` with that alias. Supply reviewed metadata-only recipe examples from `examples/recipes/`, adapted to the deployment's permitted names and targets. Do not include credentials or private-key contents in agent instructions.

```markdown
## Secret operations

Use the configured SSH broker; never call vault or target provider CLIs directly.
The broker connection is: ssh BROKER_ALIAS

Start with:
- ssh BROKER_ALIAS help
- ssh BROKER_ALIAS version
- ssh BROKER_ALIAS actions

Use reviewed recipes containing names and target metadata only, never values.
Upload a recipe with:
ssh BROKER_ALIAS write-recipe task.yaml < task.yaml

Run the lifecycle:
1. ssh BROKER_ALIAS plan --file task.yaml --save
2. ssh BROKER_ALIAS show-plan --id PLAN_ID
3. ssh BROKER_ALIAS preflight-plan --id PLAN_ID
4. For operations requiring approval, ask the human to review and approve the plan
   through their separate access. Wait for approval before attempting execution.
5. ssh BROKER_ALIAS execute-plan --id PLAN_ID

For a new secret whose value only the human has, plan a vault.secret.add recipe
and ask the human to run `secretops add --plan PLAN_ID` on their own computer.
Do not run execute-plan for it; check the result with show-plan. Never ask for
the value.

Stop on preflight failure. If preflight or execution reports a changed vault
destination, changed plan content, or "replan required", tell the human. A new
plan needs a new human approval. Never approve or reject plans yourself, inspect broker
credentials, request secret values, or retry an unknown write outcome automatically.
If execute-plan exits 3 or reports an unknown outcome, stop and tell the human.
Do not prepare a new sync plan for those names until the human has resolved the
state; a later name check does not show that the intended value was stored.
Rotation and target sync require separate plans. Production writes are currently
blocked by policy; human approval does not override that restriction.
```

`write-recipe` is a wrapper command; the other commands invoke the broker's installed CLI. The wrapper accepts only the exact command shapes documented in [BROKER_HOST.md](BROKER_HOST.md), not an arbitrary remote shell or unrestricted CLI flags. The CLI runs on demand; the agent does not start a persistent secretops service.

The current integration surface is CLI over SSH. `help` prints the allowed commands, the lifecycle and exit codes as plain text. There is no HTTP API, MCP server, or machine-readable capability/schema discovery command. Installation-time generation of consumer instructions is not implemented; the template above is copied and configured manually.

## Dummy-first rollout suggestion

Start agent trials with dummy secret names such as `NAS_BACKUP_*` or `BACKUP_*` and non-production environments.

Do not begin by writing real secrets to a NAS, remote server, production target, or other sensitive destination. First prove that the agent can plan, show, preflight, and execute safe dummy workflows without exposing values.
