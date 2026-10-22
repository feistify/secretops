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

Before approving, read the whole `show-plan` output: action, environment, vault destination, every source and target name, and the GitHub repository and environment. Do not approve a plan whose preflight failed.

## When something fails

`execute-plan` exits 0 on success, 1 when nothing uncertain happened, and 3 when a write outcome is unknown. `secretops add` exits 0 saved, 1 not written, 2 cancelled, 3 unknown.

| Message or exit | Meaning | What to do |
| --- | --- | --- |
| preflight fails | a check failed before anything was written | fix the cause (name, policy, connection), plan again |
| `plan already executed` | the plan ran before; its status is kept | to run it again, save a new plan (and approve it, for a write) |
| `replan required`, changed destination | the vault configuration changed since planning | plan again and approve the new plan |
| `vault error: ...` | token, URL or connection problem | check the token; see [renew](#renew-the-vault-token) |
| `secret missing or inaccessible` | name absent, or no permission for it | check the name, environment, folder and identity permissions |
| GitHub `not written` / `not attempted` | the sync stopped before these writes | fix the cause; a new plan writes them |
| exit 3, `outcome unknown` | a write may or may not have happened | see below |

**Exit 3.** The agent stops and tells you; it never retries. For a vault write, check the secret in Infisical. For GitHub, `verify_many` only shows whether the name exists: GitHub never returns values, so nothing can prove which value was stored. If you need certainty, approve a new sync plan for those names: it writes the current vault value again and overwrites whatever is there. Do that deliberately, after considering who uses the secret. A partial sync is not rolled back: acknowledged writes stay.

If `execute-plan` says the plan status could not be saved, do not run the plan again; inspect it with `broker show-plan` first.

## Renew the vault token

The token is renewed by hand. An expired or revoked token fails safely with `vault error`; secretops never falls back to a personal login.

1. Create a new access token for the same machine identity in Infisical.
2. **Human:**
   ```sh
   sudo secretops-configure --token-only
   ```
   It asks only for the token, at a hidden prompt, and keeps every other setting. Plans and approvals stay valid: the destination is unchanged.
3. **Human:** check with an existing secret:
   ```sh
   broker check-vault-read --name EXISTING_TEST_SECRET --env dev
   ```
4. Revoke the old token in Infisical.

To change the vault URL, project or folder, run `sudo secretops-configure` without options. Open plans for the old destination then stop with "replan required".

The GitHub login is renewed the same way it was created: `cd / && sudo -u secretops-broker gh auth login --hostname github.com`.

## Upgrade

1. Pause the agent and finish any `secretops add`.
2. **Human**, in a checkout of the new version:
   ```sh
   uv build
   sudo bash scripts/install.sh --upgrade --wheel dist/secretops-VERSION-py3-none-any.whl
   ```
   It refuses while broker processes are running. It builds and checks the new application before switching, then replaces the wrappers. `broker.env`, policy, plans, audit, the SSH service and keys are not changed. If the new version ships a different default policy, it says so and leaves yours unchanged.
3. Read the release notes: a new plan contract version means open plans must be planned and approved again.
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
sudo -u secretops-broker /usr/local/bin/secretops-broker version
```

Rollback does not change plans or audit: plans executed after the upgrade stay executed.

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

```sh
# Human
sudo secretops-uninstall
```

It lists what it removes and asks once. It removes the agent's access first, then the SSH service, the application with `broker.env` (the token), the programs, the `secretops-broker` account with its GitHub login, and the agent's broker key. Plans, audit and policy are kept in a root-only archive, `/root/secretops-uninstall-DATE.tar.gz`, which never contains the token; `--purge` skips the archive. Pause the agent first: it refuses while a broker command is running. If it stops partway, for example because the broker account cannot be removed, fix the cause and run it again; the programs are removed last.

Then revoke what stays valid outside the machine: the Infisical machine identity token, and the broker's GitHub CLI authorization in your GitHub settings under Applications. Your own `~/.config/secretops/client.yaml` is left in place.
