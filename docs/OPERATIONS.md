# Operate a local broker

Daily use and maintenance of a broker installed with [INSTALL.md](INSTALL.md). "Human" means your own terminal, never an agent session; "agent" means the agent account through `secretops-local`. In the human commands, `broker` stands for:

```sh
sudo -u secretops-broker /usr/local/bin/secretops-broker
```

## Daily workflow

| Step | Who | Command |
| --- | --- | --- |
| Store a value only you have | human | `secretops add NAME --env dev` |
| Prepare that entry instead | agent, then human | agent plans a `vault.secret.add` recipe; human runs `secretops add --plan PLAN_ID` |
| Upload a recipe | agent | `secretops-local write-recipe task.yaml < task.yaml` |
| Plan | agent | `secretops-local plan --file task.yaml --save` |
| Inspect | agent | `secretops-local show-plan --id PLAN_ID`, `secretops-local preflight-plan --id PLAN_ID` |
| Approve or reject | human | `broker show-plan --id PLAN_ID`, then `broker approve-plan --id PLAN_ID` or `broker reject-plan --id PLAN_ID` |
| Execute | agent | `secretops-local execute-plan --id PLAN_ID` |
| Check GitHub names | agent | plan and execute a `github.env_secret.verify_many` recipe |

Recipes contain names and target metadata only, never values; examples are in [`examples/recipes/`](../examples/recipes/). Vault rotation and GitHub sync are separate plans: rotating a secret does not update GitHub until a sync plan is approved and executed. Production writes are blocked by the default policy, even with approval.

To find what waits for you without copying a plan ID from the agent's terminal, run `sudo -u secretops-broker /usr/local/bin/secretops-broker pending-plans` (local broker). It lists the plans that need your approval or entry, newest last, each with the commands to review and approve it (or `secretops add --plan PLAN_ID`). It changes nothing; the agent cannot run it. The printed commands are for the local broker; with a remote broker, use your own broker access with the same subcommands. Plans without a recorded planning time (from older versions) are listed first and count as the oldest.

Before approving, read the whole `show-plan` output: action, environment, vault destination, every source and target name, and the GitHub repository and environment. Do not approve a plan whose preflight failed. Check `planned_at` too: a plan has no age limit before approval, only after it.

## When something fails

`execute-plan` exits 0 on success, 1 when it failed, or finished with a warning (status or audit not recorded), and no outcome is unknown, and 3 when a write outcome is unknown. `secretops add` exits 0 saved, 1 not written, 2 cancelled, 3 unknown.

| Message or exit | Meaning | What to do |
| --- | --- | --- |
| installer: `3.11 or newer with venv is required` | the system Python is older, for example 3.10 on Ubuntu 22.04 | install a newer Python as root and pass it with `--python PATH` ([prerequisites](INSTALL.md#prerequisites)); the system default stays as it is |
| installer: `must be an executable whose path is root-owned` for the Infisical CLI | the CLI is missing from `/usr/bin` or installed under a user's home | install the system package as root so that it is `/usr/bin/infisical` |
| Infisical repository setup ends with an `apt-get update` failure | another, unrelated APT source can fail the update after the Infisical source was added | check that the package is available: `apt-cache policy infisical` or `apt-cache madison infisical`, then install it. Never bypass signature checks; fix the other source separately |
| any command: `FAIL policy file ...` | `/opt/secretops/policy.yaml` has an unknown key (often a typo) or a wrongly typed value, or cannot be read; nothing was done | correct what the message names ([policy file](CONFIGURATION.md#policy-file)), then run the command again |
| preflight fails | a check failed before anything was written | fix the cause (name, policy, connection), plan again |
| `plan already executed` | the plan ran before; its status is kept | to run it again, save a new plan (and approve it, for a write) |
| `plan requires approval`, `not approved yet` | `execute-plan` ran before the approval; nothing was done and the plan is still `planned` | approve it (human), then execute it once |
| `approval expired`, `approval time is in the future`, `approved by an older secretops version` | the approval is older than `approval_valid_hours` (default 24), the clock moved back, or the plan was approved before the upgrade; no provider call, the plan is now `failed` | save a new plan and approve it |
| `plan is in use by another command` | another command (execute, approve, reject or `secretops add`) is working on this plan; nothing was done | wait for it to finish, check the plan with `show-plan`, then try again if still needed |
| `plan lock could not be taken` | the lock file beside the plan cannot be opened (plan directory permissions); nothing was done | fix ownership and permissions of the plan directory, then try again |
| `FAIL the audit record could not be written` with `the command stopped before any vault or GitHub write; nothing was changed` (exit 1) | the audit log cannot be written (permissions, disk space, a directory in its place); the command stopped before any provider write and changed no plan; for `approve-plan` / `reject-plan` the decision was not saved | fix the audit log location, then run the command again (or tell the agent it may); an approved plan stays approved, but its approval still expires after `approval_valid_hours` |
| `approve-plan` / `reject-plan`: `WARNING the audit record could not be written` with `the decision was saved and stands` (exit 1) | the decision was saved and stands; only its audit event is missing | fix the audit directory permissions or disk space; do not decide again, the plan is no longer `planned` |
| `replan required`, changed destination | the vault configuration changed since planning | plan again and approve the new plan |
| `vault error: ...` | the message names the cause: vault not reachable, token refused, or project, environment or folder not found or not permitted; `infisical CLI failed` means it could not tell | run `sudo secretops-configure --check` (add `--env NAME` for another environment) and follow its next step; for a refused token see [renew](#renew-the-vault-token) |
| `secret missing or inaccessible` | name absent, or no permission for it | check the name, environment, folder and identity permissions |
| GitHub `not written` / `not attempted` | the sync stopped before these writes | fix the cause; a new plan writes them |
| exit 3, `outcome unknown` | a write may or may not have happened | see below |

**Exit 3.** The agent stops and tells you; it never retries. For a vault write, check the secret in Infisical. For GitHub, `verify_many` only shows whether the name exists: GitHub never returns values, so nothing can prove which value was stored. If you need certainty, approve a new sync plan for those names: it writes the current vault value again and overwrites whatever is there. Do that deliberately, after considering who uses the secret. A partial sync is not rolled back: acknowledged writes stay.

If `approve-plan` or `reject-plan` says the decision could not be saved, the plan is unchanged and still planned: fix the plan directory (permissions, free space) and run the command again. If `execute-plan` says the plan status could not be saved, do not run the plan again; inspect it with `broker show-plan` first. If it says the audit record could not be completed, the printed results are what happened; the plan is marked `failed` even after confirmed writes, so it cannot run again. Fix the audit log location before the next operation.

## Vault behind a private CA

When the vault's certificate comes from your own CA and that CA is not in the broker host's system store:

```sh
sudo secretops-configure --ca-file /etc/secretops/vault-ca.pem
```

The file must be a PEM certificate owned by root, readable by every account (it is public) and not writable by others, in a directory that only root can change (not `/tmp`), with no symbolic link on the path: whoever can replace it can make the broker send the token to a forged vault. The broker checks this again at every vault call and stops if it no longer holds. The setting applies only to the vault: the Infisical CLI and the connection check use it, while `gh` keeps the system store for GitHub. The vault check runs right after. After a CA rotation, replace the file and run `sudo secretops-configure --check`; `sudo secretops-configure --no-ca-file` removes the setting.

## Renew the vault token

The token is renewed by hand. An expired or revoked token fails safely with `vault error`; secretops never falls back to a personal login.

1. Create a new access token for the same machine identity in Infisical.
2. **Human:**
   ```sh
   sudo secretops-configure --token-only
   ```
   It asks only for the token, at a hidden prompt, and keeps every other setting. Plans and approvals stay valid: the destination is unchanged.
3. The vault check runs automatically after `--token-only` (again: `sudo secretops-configure --check`). To also confirm the folder, check an existing secret:
   ```sh
   broker check-vault-read --name EXISTING_TEST_SECRET --env dev
   ```
4. Revoke the old token in Infisical.

To change the vault URL, project or folder, run `sudo secretops-configure` without options. Open plans for the old destination then stop with "replan required".

The GitHub login is renewed the same way it was created: `cd / && sudo -u secretops-broker gh auth login --hostname github.com`.

## Upgrade

1. Pause the agent and finish any `secretops add`.
2. **Human:** download, check and unpack the new version's installer bundle as in [INSTALL.md](INSTALL.md#install), then in its directory:
   ```sh
   sudo ./install.sh --upgrade
   ```
   It refuses while broker processes are running. It builds and checks the new application before switching, then replaces the wrappers. `broker.env`, policy, plans, audit, the SSH service and keys are not changed. It keeps a Python given with `--python` at installation; pass `--python PATH` to change it. If the new version ships a different default policy, it says so and leaves yours unchanged. If the new version refuses the policy the broker uses (the one `broker.env` names; for example a key it does not know or a quoted `"false"`; earlier versions accepted both), the upgrade stops before switching with `the new version refuses the configured policy` below the reason; nothing was changed. Correct what it names and run the upgrade again.
3. Read the release notes: a new plan contract version means open plans must be planned and approved again. Upgrading from a version without approval times (0.1.9 and earlier) is the same for approved plans that have not run: they are refused and must be planned and approved again; `planned` plans keep working. The upgrade keeps your policy file: to run the read-only checks in `prod`, add `prod` to `allowed_environments` of `vault.read.check`, `vault.secret.diff_envs` and `github.env_secret.verify_many` there; `approval_valid_hours` and `production_environments` are optional ([policy file](CONFIGURATION.md#policy-file)). The production refusal now ends with `(environment NAME is a production environment by policy)`. From this version on, a vault environment name outside the allowed form (letters, digits, `.`, `-` and `_`, starting with a letter or digit, at most 64 characters) is refused in recipes, saved plans and the policy: plan such work again with a corrected name, and correct the policy; the upgrade stops before switching when the configured policy is refused ([policy file](CONFIGURATION.md#policy-file)).
4. **Agent:** `secretops-local version`.

### Roll back an upgrade

The previous application stays in `/opt/secretops/venv.previous` and the previous wrappers in `/opt/secretops/previous/`, until the next upgrade.

```sh
# Human, with the agent paused
sudo mv /opt/secretops/venv /opt/secretops/venv.failed
sudo mv /opt/secretops/venv.previous /opt/secretops/venv
for name in secretops-broker secretops-agent secretops-local; do
    sudo install -o root -g root -m 0755 /opt/secretops/previous/$name /usr/local/bin/$name
done
[ ! -f /opt/secretops/previous/secretops-uninstall ] || sudo install -o root -g root -m 0755 \
    /opt/secretops/previous/secretops-uninstall /usr/local/sbin/secretops-uninstall
sudo -u secretops-broker /usr/local/bin/secretops-broker version
```

Rollback does not change plans or audit: plans executed after the upgrade stay executed. Before rolling back past v0.1.10, remove `approval_valid_hours` and `production_environments` from the policy file (the older version refuses unknown keys), and plan again anything saved after the upgrade: the older version refuses those plans as not matching its schema.

## Back up

Back up plans, audit and policy. Do **not** back up `broker.env` (it holds the token), the broker's GitHub login, or SSH keys: after a restore you configure and log in again.

```sh
# Human, with the agent paused and no broker command running
sudo tar -C /opt/secretops -czf /root/secretops-backup-$(date +%F).tar.gz plans audit policy.yaml
sudo chmod 0600 /root/secretops-backup-*.tar.gz
```

Keep the archive where the agent cannot read it. It contains names, destinations and approvals, but no values.

## Restore

After a fresh installation (see [INSTALL.md](INSTALL.md)), with the agent paused. The archive was made from broker-owned directories, so check it before root extracts or copies anything: it must contain only plain files and directories with the expected names. Both check commands must print nothing; if either prints a line, stop.

```sh
# Human
archive=/root/secretops-backup-DATE.tar.gz
sudo tar -tvzf "$archive" | awk '$1 !~ /^[-d]/'                               # links, devices
sudo tar -tzf "$archive" | grep -Ev '^(plans|audit)/([^/]+)?$|^policy\.yaml$'   # other paths
sudo mkdir -m 0700 /root/secretops-restore
sudo tar -C /root/secretops-restore --no-same-owner -xzf "$archive"
sudo install -o root -g root -m 0644 /root/secretops-restore/policy.yaml /opt/secretops/policy.yaml
sudo install -o secretops-broker -g secretops-broker -m 0600 \
    /root/secretops-restore/audit/secretops.jsonl /opt/secretops/audit/secretops-restored-DATE.jsonl
sudo mv /root/secretops-restore/plans /root/secretops-restored-plans
```

The restored audit log gets its own name, so it never overwrites newer records. Restored plans are kept as history only, not put back into `/opt/secretops/plans`: the backup can be older than writes that happened later, so an old approval must not be executed again. Plan every pending operation again and approve it. Then run `sudo secretops-configure` and the GitHub login. Vault data lives in Infisical and is not part of this backup.

## Uninstall

First write down the vault URL, project ID and folder (not the token); you need them to configure a new installation.

```sh
# Human
sudo secretops-uninstall
```

It lists what it removes and asks once. It removes the agent's access first, then the SSH service, the application with `broker.env` (the token), the programs, the `secretops-broker` account with its GitHub login, and the agent's broker key. Plans, audit and policy are kept in a root-only archive, `/root/secretops-uninstall-DATE.tar.gz`, which never contains the token; `--purge` skips the archive. Pause the agent first: it refuses while a broker command is running. If it stops partway, for example because the broker account cannot be removed, fix the cause and run it again; the programs are removed last.

Then revoke what stays valid outside the machine: the Infisical machine identity token, and the broker's GitHub CLI authorization in your GitHub settings under Applications. Your own `~/.config/secretops/client.yaml` is left in place. An agent account created with `--create-agent-user` is kept with its files; remove it with `sudo userdel -r NAME` when you no longer need it.
