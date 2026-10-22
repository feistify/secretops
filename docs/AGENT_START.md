# Start here: coding agents

You are a coding agent, and a human asked you to install secretops or to use it for their project. This page tells you what you may do, what only the human does, and which page to read next. If you are developing secretops itself, read [AGENTS.md](../AGENTS.md) instead.

## The rules

secretops lets you manage secrets by name while the values stay with the human, the vault and the target. You lose that only by working around it, so:

- Never ask for, read, print or store a secret value, token, key or device code. If the human offers one, tell them not to paste it.
- Never run `sudo`, never enter a token, and never complete a login for the human, even if your session allows it. Prepare the command; the human runs it in their own terminal.
- Never call `infisical`, `gh secret`, `env` or `printenv`, and never read `.env` files or `/opt/secretops/broker.env`.
- Never approve or reject a plan. Never retry a write whose outcome is unknown (exit 3).

## Find out where you are

```sh
command -v secretops-local && secretops-local help
```

- **Command not found:** the broker is not installed for your account. Go to [Help the human install](#help-the-human-install).
- **Prints the command list:** the broker connection works. Go to [Use the broker](#use-the-broker).
- **Prints `denied: subcommand not allowed: help`:** the broker works but is older than this guide; `secretops-local version` shows its version. Use the commands below, without `help`.
- **Prints an SSH or permission error:** the broker exists, but your account is not set up as its agent. Tell the human; the installer's `--agent-user` must be the account you run as.

## Help the human install

The installation guide is [INSTALL.md](INSTALL.md). Every command block there says who runs it. You may:

- Check prerequisites and report what is missing: Ubuntu 24.04 or Debian 12, Python 3.11 or newer, `python3-venv`, `openssh-server`, `curl`, a root-owned `infisical` (for example `/usr/bin/infisical`) and, for GitHub sync, a root-owned `gh`.
- Find your account name (`id -un`) and whether it can use `sudo`. The recommended setup runs you as a separate account without sudo, which the installer can create with `--create-agent-user`; if you run as the human's own account, tell them that the boundary is then a convention, not enforced.
- Write out the exact commands from INSTALL.md with the values filled in (version, agent account, Infisical URL), for the human to run.
- After installation, run `secretops-local help`, `secretops-local version` and `secretops-local actions`.

Only the human:

- downloads and checks the installer bundle and runs `sudo ./install.sh ...`, `sudo secretops-configure` (asks for the Infisical token) and `sudo -u secretops-broker gh auth login` (GitHub device login);
- creates the Infisical project and machine identity;
- enters secret values and approves plans;
- removes the broker with `sudo secretops-uninstall`.

## Use the broker

You reach the broker only through `secretops-local`, with these exact command shapes:

| Command | What it does |
| --- | --- |
| `secretops-local help` | the allowed commands, workflow, exit codes and rules |
| `secretops-local version` | broker version |
| `secretops-local actions` | actions the current policy allows and denies |
| `secretops-local write-recipe NAME.yaml < FILE` | upload a recipe (names and metadata only) |
| `secretops-local plan --file NAME.yaml --save` | save a plan from an uploaded recipe; prints `plan_id` |
| `secretops-local show-plan --id PLAN_ID` | show the plan and its status |
| `secretops-local preflight-plan --id PLAN_ID` | check prerequisites without writing |
| `secretops-local execute-plan --id PLAN_ID` | run an approved plan |

Anything else is refused. Recipe names use lowercase letters, digits, `.`, `-` and `_`, end in `.yaml`, and do not start with a dot.

### The plan lifecycle

1. Write a recipe, upload it, and save a plan.
2. Run `show-plan` and `preflight-plan`. If preflight fails, stop and tell the human why.
3. If the action needs approval, ask the human to review and approve it, and give them the commands:
   ```sh
   sudo -u secretops-broker /usr/local/bin/secretops-broker show-plan --id PLAN_ID
   sudo -u secretops-broker /usr/local/bin/secretops-broker approve-plan --id PLAN_ID
   ```
   Wait until they say it is approved. Under the default policy, metadata checks (`vault.read.check`, `vault.secret.diff_envs`, `github.env_secret.verify_many`) need no approval.
4. Run `execute-plan`. Exit 0: done. Exit 1: failed, nothing uncertain; tell the human. Exit 3: a write outcome is unknown; stop and tell the human, do not retry and do not plan the same names again.

A plan runs once. To repeat an operation, save a new plan. Production writes are refused by policy, even with approval.

### When the value only exists with the human

For a new secret such as an API key from a provider console, plan a `vault.secret.add` recipe and ask the human to run `secretops add --plan PLAN_ID` in their own terminal. They see the destination, type the value at a hidden prompt and confirm; that confirmation is the approval. Do not run `execute-plan` for this plan; check the result with `show-plan` (`executed`, or `failed` with a reason).

```yaml
action: vault.secret.add
secret_name: STRIPE_KEY
env: staging
```

The human can also do it without your plan: `secretops add STRIPE_KEY --env staging`.

### Recipes

The [recipe reference](RECIPES.md) lists every action with its fields, whether it needs approval, the allowed environments and an example. Replace the example owner, repository, environments and names with the human's. The vault project and folder are set by the human in the broker, not in recipes. Every environment a recipe names, including the GitHub environment, must be one the policy allows (by default `dev` and `staging` for writes).

Rotation changes only the vault; syncing to GitHub is a separate plan. GitHub never returns values: `verify_many` proves that a name exists, not which value it has.

### When something fails

Messages and what to do about them are in [OPERATIONS.md](OPERATIONS.md#when-something-fails). In short: on `replan required` or a changed destination, save a new plan and ask for a new approval; on `vault error`, tell the human to check the token; on `plan already executed`, save a new plan.

## Set up the human's project

So that later sessions use the broker too, add the [project instructions](AGENT_USAGE.md#instructions-for-agents-in-consumer-projects) to the project's `AGENTS.md` (or `CLAUDE.md`), with `ssh BROKER_ALIAS` replaced by `secretops-local`. Show the human the change before you commit it.

## Try it first

With a test Infisical project, a test GitHub repository and made-up values, walk through the [demo](DEMO.md) with the human before touching real secrets.
