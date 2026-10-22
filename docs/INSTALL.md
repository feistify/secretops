# Install a local broker

This guide installs secretops as a local broker on one Linux machine: the human and the coding agent use the same machine, the broker runs as its own account, and the agent reaches it only through a restricted loopback SSH key. Daily use, renewal, upgrade and backup are in [OPERATIONS.md](OPERATIONS.md). The model and its reasons are in [BROKER_HOST.md](BROKER_HOST.md).

Every command block says who runs it. "Human" means your own terminal, not a terminal or tool session of the agent: never run `sudo` or enter a token through an agent (for example Claude Code's `!` prefix).

Tested: Ubuntu 24.04 with its Python 3.12, installer checks in a clean Ubuntu 24.04 container (`tests/test_install_container.sh`). A clean-VM run with a self-hosted Infisical and GitHub is recorded in [local-broker-installation.md](validation/local-broker-installation.md). Other distributions are not tested.

## What gets installed

| Path | Owner | Contents |
| --- | --- | --- |
| `/opt/secretops/venv`, `bin/`, `policy.yaml`, `ssh/` | root | application, policy, SSH service configuration and host key |
| `/opt/secretops/broker.env` | `secretops-broker`, 0600 | vault URL, project, folder, token, pinned paths |
| `/opt/secretops/plans`, `audit`, `recipes` | `secretops-broker`, 0700 | saved plans, audit log, uploaded recipes |
| `/usr/local/bin/secretops-broker`, `secretops-agent`, `secretops-local`, `secretops` | root | broker wrapper, agent forced command, agent client, human client |
| `/usr/local/sbin/secretops-configure` | root | human-only configuration and token helper |
| `secretops-local-sshd.service` | root | SSH listener on `127.0.0.1:22222` only, broker account, agent key only |
| agent's `~/.ssh/secretops_local_*` | agent | agent key and the pinned broker host key |
| human's `~/.config/secretops/client.yaml` | human | how `secretops add` reaches the broker (local sudo) |

The installer never asks for, reads or writes a credential, and never calls Infisical or GitHub.

## Accounts

- **Human**: your normal login with sudo. You configure credentials and approve plans.
- **Broker**: `secretops-broker`, created by the installer. Holds the token and the GitHub login.
- **Agent**: the account the coding agent runs as.

**Recommended:** run the agent as a separate account without sudo. Then the agent cannot use your sudo, SSH keys or terminal, and approval stays with you:

```sh
# Human
sudo adduser --disabled-password --gecos '' agent      # not in the sudo group
sudo -iu agent git clone https://github.com/OWNER/PROJECT.git
```

Give that account only the Git access its work needs: a public clone, or a separately scoped credential that cannot write the repository's environment secrets. Install and log in to the coding agent CLI as that account, following the vendor's instructions. Start the agent with a separate login, for example `ssh agent@localhost` or a second console. Approve and enter secrets from your own terminal.

**Simpler, weaker:** use your own account as `--agent-user`. The restricted SSH key still limits what the agent can do *through the broker*, but an agent running as you can use your sudo, keys and terminal. The approval boundary is then a convention, not enforced. The installer prints a warning in this case.

## Prerequisites

```sh
# Human
sudo apt install python3-venv openssh-server git
```

- **OpenSSH server**: on Ubuntu, installing `openssh-server` also starts the normal network-facing `ssh` service. secretops does not use it. If you do not want it: `sudo systemctl disable --now ssh.service ssh.socket`.
- **Infisical CLI**: install it as root following the [official instructions](https://infisical.com/docs/cli/overview), so that it is `/usr/bin/infisical` or another root-owned path. The installer refuses a CLI that the broker or another user could replace. The tested CLI version is in the validation records.
- **Infisical project and machine identity**: an Infisical Cloud account or a self-hosted instance ([self-hosting](https://infisical.com/docs/self-hosting/overview)). Installing secretops does not install Infisical. Create a project and a [machine identity](https://infisical.com/docs/documentation/platform/identities/machine-identities) with access only to that project and the environments you use (for example `dev` and `staging`), able to read and write secrets there. Use [token auth](https://infisical.com/docs/documentation/platform/identities/token-auth) or another method that gives you an access token; note when it expires.
- **GitHub CLI** (only for GitHub sync): `gh` installed as root, for example from the [official package](https://github.com/cli/cli/blob/trunk/docs/install_linux.md).
- **uv** (to build the wheel): see the [uv installation](https://docs.astral.sh/uv/getting-started/installation/).

## Install

```sh
# Human
git clone --branch v0.1.2 https://github.com/feistify/secretops.git
cd secretops
uv build
sudo bash scripts/install.sh --wheel dist/secretops-0.1.2-py3-none-any.whl \
    --agent-user agent --infisical-domain https://app.infisical.com
```

Options: `--infisical-bin PATH` (default `/usr/bin/infisical`), `--gh-bin PATH` (default `/usr/bin/gh` if present), `--port PORT` (default 22222). The installer refuses to overwrite an existing installation, account, port, or agent key. The last lines check that the agent reaches the broker:

```text
PASS: agent reaches the broker through the restricted SSH key.
Installed secretops 0.1.2. Vault credentials are not configured yet.
```

The installer downloads the Python dependencies from PyPI with the hashes pinned in `scripts/broker-requirements.txt`.

## Configure the vault

```sh
# Human
sudo secretops-configure
```

It asks for the vault URL, project ID and folder path, then for the access token at a hidden prompt. The token is not shown, not put in an argument or shell history, and written only to `broker.env`. Then check the connection with a secret that already exists in the project, for example one you created in the Infisical UI:

```sh
# Human
sudo -u secretops-broker /usr/local/bin/secretops-broker check-vault-read --name EXISTING_TEST_SECRET --env dev
```

Names starting with `SECRETOPS_`, `INFISICAL_`, `LC_`, `XDG_`, `LD_` or `PYTHON`, and a few environment names such as `HOME` and `PATH`, are reserved and refused before any vault call; use an ordinary name such as `VM_CHECK_KEY`. `OK secret exists: ...` means the token, project and folder work. `secret missing or inaccessible` for a name that exists means the folder, environment or permissions are wrong. `vault error: ...` means the token or URL is wrong. Only an existing name proves that the setup works.

## Log the broker in to GitHub (only for GitHub sync)

```sh
# Human
cd /
sudo -u secretops-broker gh auth login --hostname github.com
```

Choose HTTPS and browser login. If the browser does not open, open `https://github.com/login/device` and enter the code yourself. Never give the code to the agent. The login must allow writing environment secrets of the target repository; details are in [BROKER_HOST.md](BROKER_HOST.md#github-authentication-human-only).

## Set up the agent

The agent uses `secretops-local` as its broker connection: it is `ssh` with the installed key, the pinned host key, and no prompts.

```sh
# Agent account
secretops-local version
secretops-local actions
```

Copy the [consumer-project instructions](AGENT_USAGE.md#instructions-for-agents-in-consumer-projects) into the agent's project `AGENTS.md` (or `CLAUDE.md`) and replace `ssh BROKER_ALIAS` with `secretops-local`.

## First workflow

Follow the [README demo](../README.md#demo-enter-once-sync-through-an-approved-plan) with synthetic data in a test Infisical project and a test GitHub repository with a `dev` environment. It covers human entry, agent planning, human review and approval, sync, metadata verification and cleanup. Run human commands in your own terminal and agent commands as the configured agent account.

## Remove a failed installation

If the installer stops partway, it changed only these paths. Inspect them, then remove them before retrying:

```sh
# Human
sudo systemctl disable --now secretops-local-sshd.service 2>/dev/null
sudo rm -f /etc/systemd/system/secretops-local-sshd.service /usr/local/bin/secretops \
    /usr/local/bin/secretops-broker /usr/local/bin/secretops-agent /usr/local/bin/secretops-local \
    /usr/local/sbin/secretops-configure
sudo rm -rf /opt/secretops /var/lib/secretops-broker
sudo userdel secretops-broker
# In the agent account: rm ~/.ssh/secretops_local_agent_ed25519* ~/.ssh/secretops_local_known_hosts
```

Do not do this for a working installation that has plans or audit you want to keep; see [OPERATIONS.md](OPERATIONS.md#uninstall).

## Limits

- One broker per machine, one Infisical project and folder per broker.
- Human secret entry over SSH from another machine, tmux, and other distributions are not tested.
- The token is renewed by hand; see [OPERATIONS.md](OPERATIONS.md#renew-the-vault-token).
- `broker.env` is read by the broker wrapper as shell. Edit it only through `secretops-configure`, which accepts and writes only plain `KEY='value'` lines.
