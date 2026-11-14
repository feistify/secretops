# Install a local broker

This guide installs secretops as a local broker on one Linux machine: the human and the coding agent use the same machine, the broker runs as its own account, and the agent reaches it only through a restricted loopback SSH key. Daily use, renewal, upgrade and backup are in [OPERATIONS.md](OPERATIONS.md). The model and its reasons are in [BROKER_HOST.md](BROKER_HOST.md).

Every command block says who runs it. "Human" means your own terminal, not a terminal or tool session of the agent: never run `sudo` or enter a token through an agent (for example Claude Code's `!` prefix).

Tested: Ubuntu 24.04 with its Python 3.12; installer checks in clean Ubuntu 24.04, Debian 12 and Ubuntu 22.04 (with Python 3.12 from the deadsnakes PPA) containers (`tests/test_install_container.sh`). A clean-VM run with a self-hosted Infisical and GitHub is recorded in [local-broker-installation.md](validation/local-broker-installation.md). Other distributions are not tested.

## What gets installed

| Path | Owner | Contents |
| --- | --- | --- |
| `/opt/secretops/venv`, `bin/`, `policy.yaml`, `ssh/` | root | application, policy, SSH service configuration and host key |
| `/opt/secretops/broker.env` | `secretops-broker`, 0600 | vault URL, project, folder, token, pinned paths |
| `/opt/secretops/plans`, `audit`, `recipes` | `secretops-broker`, 0700 | saved plans, audit log, uploaded recipes |
| `/usr/local/bin/secretops-broker`, `secretops-agent`, `secretops-local`, `secretops` | root | broker wrapper, agent forced command, agent client, human client |
| `/usr/local/sbin/secretops-configure` | root | human-only configuration and token helper |
| `/usr/local/sbin/secretops-uninstall` | root | human-only removal; see [OPERATIONS.md](OPERATIONS.md#uninstall) |
| `secretops-local-sshd.service` | root | SSH listener on `127.0.0.1:22222` only, broker account, agent key only |
| agent's `~/.ssh/secretops_local_*` | agent | agent key and the pinned broker host key |
| human's `~/.config/secretops/client.yaml` | human | how `secretops add` reaches the broker (local sudo) |

The installer also creates the `secretops-broker` account (home `/var/lib/secretops-broker`) and, with `--create-agent-user`, the agent account, then enables and starts the service. It never asks for, reads or writes a credential, and never calls Infisical or GitHub. It installs no system packages and does not change the system Python or which Python is the default, the system `ssh` service or its configuration, sudo rules, or any account's shell configuration.

## Accounts

- **Human**: your normal login with sudo. You configure credentials and approve plans.
- **Broker**: `secretops-broker`, created by the installer. Holds the token and the GitHub login.
- **Agent**: the account the coding agent runs as.

**Recommended:** run the agent as a separate account without sudo. Then the agent cannot use your sudo, SSH keys or terminal, and approval stays with you:

The installer can create it: `--create-agent-user agent` in [Install](#install). It creates a normal account without sudo and without a password. Or create it yourself:

```sh
# Human
sudo adduser --disabled-password --gecos '' agent      # not in the sudo group
```

Then, as that account, clone your project:

```sh
# Human
sudo -iu agent git clone https://github.com/OWNER/PROJECT.git
```

Give that account only the Git access its work needs: a public clone, or a credential of its own for that repository, such as a deploy key or an HTTPS token created for the agent account only, that cannot write the repository's environment secrets. Never give it your own keys or tokens.

Start the coding agent from a shell of that account, in a terminal window you keep for the agent: open it with `sudo -iu agent`, install the coding agent there without sudo (the vendor's per-user installation), log it in, `cd` into the project and start it. Approve and enter secrets from your own terminal, not from that one. Do this only if `sudo -l` lists `use_pty` among its defaults: it gives that shell its own pseudo-terminal, so a program in it cannot type into your terminal. If it is not listed, start the agent from a separate login of that account instead, for example a second console (the account then needs a password).

Do not give the agent account access to your graphical session (do not run `xhost` for it): under X11 that would let it read your keystrokes, including secret values you type. A message such as `Authorization required, but no authorization protocol specified` in the agent's shell usually comes from this refused access and is harmless. For the same reason copy and paste between the two accounts may not work; the agent keeps the plan ID, so you do not need to copy it.

**Simpler, weaker:** use your own account as `--agent-user`. The restricted SSH key still limits what the agent can do *through the broker*, but an agent running as you can use your sudo, keys and terminal. The approval boundary is then a convention, not enforced. The installer prints a warning in this case.

What each setup protects is compared in [deployment levels](SECURITY_MODEL.md#deployment-levels).

## Prerequisites

```sh
# Human
sudo apt install python3-venv openssh-server curl
```

- **Ubuntu 22.04**: its Python is 3.10; secretops needs 3.11 or newer. Install a newer Python as root and pass it with `--python` in [Install](#install), for example from the [deadsnakes PPA](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa): `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv`, then `--python /usr/bin/python3.12`.
- **OpenSSH server**: on Ubuntu, installing `openssh-server` also starts the normal network-facing `ssh` service. secretops does not use it. If you do not want it: `sudo systemctl disable --now ssh.service ssh.socket`.
- **Infisical CLI**: install it as root following the [official instructions](https://infisical.com/docs/cli/overview), so that it is `/usr/bin/infisical` or another root-owned path. The installer refuses a CLI that the broker or another user could replace. The tested CLI version is in the validation records.
- **Infisical project and machine identity**: prepared as in [PREPARE.md](PREPARE.md). Installing secretops does not install Infisical.
- **GitHub CLI** (only for GitHub sync): `gh` installed as root, for example from the [official package](https://github.com/cli/cli/blob/trunk/docs/install_linux.md). That page recommends the official packages and warns that the community-distributed 2.45.x / 2.46.x versions are broken. Tested versions are in [LIMITATIONS.md](LIMITATIONS.md).

Check that each CLI is where the installer accepts it:

```sh
# Human
namei -l "$(readlink -f /usr/bin/infisical)"
namei -l "$(readlink -f /usr/bin/gh)"            # only for GitHub sync
```

The installer follows symlinks to the real file and requires that file to be executable, and the file and every directory above it to be owned by `root` and not writable by group or others. In the output, every line after `f:` must show owner `root` and no `w` in the group and other columns (for example `drwxr-xr-x root root usr`), and the last line must contain `x`. `/usr/bin/infisical` is required; `/usr/bin/gh` is used if it exists, otherwise the installer configures no GitHub CLI; if it exists but fails these checks, the installer stops. If a CLI is elsewhere, check that path instead and pass it with `--infisical-bin PATH` or `--gh-bin PATH` in [Install](#install).

## Install

Download the installer bundle of the release, check it, unpack it and run it:

```sh
# Human
curl -fsSLO https://github.com/feistify/secretops/releases/download/v0.1.7/secretops-0.1.7-installer.tar.gz
sha256sum secretops-0.1.7-installer.tar.gz      # must match the SHA-256 in the release notes
tar -xzf secretops-0.1.7-installer.tar.gz && cd secretops-0.1.7-installer
sudo ./install.sh --create-agent-user agent --infisical-domain https://app.infisical.com
# use the address of your own Infisical: EU Cloud https://eu.infisical.com or your self-hosted URL (PREPARE.md)
```

For an existing agent account use `--agent-user NAME` instead of `--create-agent-user NAME`. Other options: `--python PATH` (default `/usr/bin/python3`; a root-owned Python 3.11 or newer with venv, kept by upgrades), `--infisical-bin PATH` (default `/usr/bin/infisical`), `--gh-bin PATH` (default `/usr/bin/gh` if present), `--port PORT` (default 22222), `--allow-untested-os` for distributions other than Ubuntu and Debian. The installer checks the system first and refuses to overwrite an existing installation, account, port, or agent key. The last lines check that the agent reaches the broker:

```text
PASS: agent reaches the broker through the restricted SSH key.
Installed secretops 0.1.7. Vault credentials are not configured yet.
```

The installer installs secretops 0.1.7 from PyPI, checked against the SHA-256 pinned in the bundle's `secretops-release.txt`, and its dependencies with the hashes pinned in `broker-requirements.txt`. With GitHub CLI you can also check where the bundle was built: `gh attestation verify secretops-0.1.7-installer.tar.gz --repo feistify/secretops`. To install from a repository checkout instead, see [DEVELOPMENT.md](DEVELOPMENT.md#install-a-broker-from-a-checkout).

## Configure the vault

```sh
# Human
sudo secretops-configure
```

It asks for the vault URL (Enter keeps the `--infisical-domain` address), project ID and folder path (Enter keeps `/`, the project root; see [PREPARE.md](PREPARE.md#infisical-steps)), then for the access token at a hidden prompt. The token is not shown, not put in an argument or shell history, and written only to `broker.env`. Its last line suggests a `check-vault-read` command; the first check below creates the secret it needs.

## First check: vault only

This needs no GitHub. Use a made-up name and value in `dev`, an environment the default policy allows ([environment names](PREPARE.md#environment-names)).

1. Store a throwaway test secret. Type any made-up single-line value at the hidden prompt:

   ```sh
   # Human
   secretops add FIRST_CHECK_KEY --env dev
   # Your sudo password may be asked first, then the hidden value and Save? [y/N]
   # Saved and verified.
   ```

2. Let the agent check it by name. `vault.read.check` needs no approval under the default policy. These commands run as the agent account; to open a shell as it, use `sudo -iu NAME` (with `--create-agent-user`, the installer's final message shows this line):

   ```sh
   # Agent account
   cat > first-check.yaml <<'YAML'
   action: vault.read.check
   secret_name: FIRST_CHECK_KEY
   env: dev
   YAML
   secretops-local write-recipe first-check.yaml < first-check.yaml
   secretops-local plan --file first-check.yaml --save      # prints plan_id
   secretops-local execute-plan --id PLAN_ID
   # OK secret exists: FIRST_CHECK_KEY
   ```

   Replace `PLAN_ID` with the printed `plan_id`. You can run the same check yourself: `sudo -u secretops-broker /usr/local/bin/secretops-broker check-vault-read --name FIRST_CHECK_KEY --env dev`.

3. Delete `FIRST_CHECK_KEY` from the `dev` environment in the Infisical web UI. secretops never deletes secrets.

What the results mean:

- `OK secret exists: ...`: the token, vault URL, project, environment and folder work.
- `vault error: ...` (from `secretops add` as `Not saved: vault error: ...`): the Infisical CLI call failed. Check the token, the vault URL, the project ID, that the environment exists in your project, and that the machine identity has access to it. If your project has no `dev` environment, use your own environment name ([environment names](PREPARE.md#environment-names)).
- `secret missing or inaccessible: ...`: the call succeeded, but the name was not among the secrets loaded for that environment and folder. Check the name, the environment and the folder path.
- `Secret already exists`: the name is already in that environment; nothing was written.
- Other `Not saved: ...` messages say why, for example a reserved or invalid name ([name rules](RECIPES.md#rules-for-all-recipes)).

The vault part works now. GitHub login and sync below are optional; skip them if you only use the vault.

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
secretops-local help
secretops-local version
secretops-local actions
```

Copy the [consumer-project instructions](AGENT_USAGE.md#instructions-for-agents-in-consumer-projects) into the agent's project `AGENTS.md` (or `CLAUDE.md`) and replace `ssh BROKER_ALIAS` with `secretops-local` and `HUMAN_BROKER` with `sudo -u secretops-broker /usr/local/bin/secretops-broker`.

## First workflow

If you have not done them yet, first do [Configure the vault](#configure-the-vault) and [First check: vault only](#first-check-vault-only). Then follow the [demo](DEMO.md) with synthetic data in a test Infisical project and a test GitHub repository with a `dev` environment. It covers human entry, agent planning, human review and approval, sync, metadata verification and cleanup. Run human commands in your own terminal and agent commands as the configured agent account.

## Remove a failed installation

If the installer stops partway, it changed only these paths. If you configured the vault, first write down the vault URL, project ID and folder (not the token). Inspect the paths, then remove them before retrying:

```sh
# Human
sudo systemctl disable --now secretops-local-sshd.service 2>/dev/null
sudo rm -f /etc/systemd/system/secretops-local-sshd.service /usr/local/bin/secretops \
    /usr/local/bin/secretops-broker /usr/local/bin/secretops-agent /usr/local/bin/secretops-local \
    /usr/local/sbin/secretops-configure /usr/local/sbin/secretops-uninstall
sudo rm -rf /opt/secretops /var/lib/secretops-broker
sudo userdel secretops-broker
# In the agent account: rm ~/.ssh/secretops_local_agent_ed25519* ~/.ssh/secretops_local_known_hosts
```

Do not do this for a working installation that has plans or audit you want to keep; see [OPERATIONS.md](OPERATIONS.md#uninstall).

## Limits

- One broker per machine, one Infisical project and folder per broker.
- The agent runs on the broker machine. A broker on another machine is a manual setup ([BROKER_HOST.md](BROKER_HOST.md)).
- An HTTP proxy cannot be configured on an installed broker.
- Human secret entry over SSH from another machine, tmux, and other distributions are not tested.
- The token is renewed by hand; see [OPERATIONS.md](OPERATIONS.md#renew-the-vault-token).
- `broker.env` is read by the broker wrapper as shell. Edit it only through `secretops-configure`, which accepts and writes only plain `KEY='value'` lines.
