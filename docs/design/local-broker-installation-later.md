# Deferred proposal: managed broker installation and operations

Status: deferred reference, not the active implementation scope. Preserved from the broader design through `0b72a8e`. See [the minimal installation plan](local-broker-installation.md). No capabilities or security guarantees described here should be treated as implemented.
Depends on: destination-bound approvals, interactive entry and GitHub target reliability (PRs #6–#9)

## Goal and completion

A person following only repository/release instructions can install a broker in a clean VM, run an agent under a separate Unix account, complete the synthetic entry-to-GitHub workflow, renew credentials and follow tested maintenance/recovery procedures. No required helper lives only in an undocumented `/tmp` directory or on the original developer's machine.

Completion requires documented **and VM-tested procedures**, not automation of every procedure. Restore and uninstall may remain manual. The work is delivered in three reviewable PRs; the whole installation milestone stays open until clean-VM validation and its fixes land.

## Supported path and boundaries

| Decision | First supported path |
| --- | --- |
| Platform | Ubuntu 24.04 LTS x86-64 VM, CPython 3.12, administrator-managed venv |
| Accounts | Distinct human administrator, unprivileged agent user, and broker service user |
| Human channel | Local CLI with password-authenticated sudo to the broker; hidden value entry in a separate human terminal |
| Agent channel | Existing restricted SSH forced command through a dedicated loopback service with pinned host identity |
| Distribution | One release bundle: wheel, locked compatible dependencies, matching wrappers/helpers, manifest and checksums |
| Providers | Existing Infisical Cloud/self-hosted instance and a GitHub test repository/environment; human provisions scoped credentials |
| Maintenance | Documented manual upgrade/rollback, credential-free backup, archive-only plan restore and state-preserving uninstall |

The agent account has no sudo/root/broker elevation rights and no access to human SSH agents, credentials or login sessions. The human may administer the machine; the agent must not be a member of privileged groups or inherit equivalent ACL/sudo access. The installer does not modify the human's general sudo policy or attempt to police every local administrative permission.

A shared human/agent UID is a reduced-assurance manual alternative, **outside the validated isolation guarantee**. A password prompt, TTY test or sudo timeout cannot isolate hostile processes under the same UID. Remove shared-login sudoers rewriting and shared-login VM qualification from this milestone. If used manually, document the risk, prohibit NOPASSWD/cached human elevation as an assumed safety boundary, and do not present it as the supported separate-account topology.

Human broker/admin sudo, credential login and hidden entry must never run inside an agent session: no Claude Code `!` prefix, agent tool terminal, or terminal currently hosting the agent. Use a separate human terminal. This rule complements UID separation; it does not replace it.

Remote human entry, remote-host installation, tmux qualification, other platforms/interpreters, automatic credential renewal, a provider installer, automatic rollback, automated restore/uninstall and OpenBao are deferred. The existing laptop installation is untouched during development; it is not the clean-VM acceptance environment. Agent SSH metadata/execution has already been exercised there; human SSH value entry has not.

## Make the agent account usable

The supported setup is incomplete unless the guide shows how an actual agent runs under the separate UID. The installer accepts an explicit existing agent account and public key; it does not discover private keys or copy the human's home/login state.

INSTALL.md must give a short human-operated account setup before installation:

1. Create an unprivileged account (example `secretops-agent-user`), private home and working directory, with no sudo membership or passwordless privilege grants.
2. Use a separate source checkout owned by that account as the default. A shared group checkout is an optional manual variant with clear permissions; never make broker state writable to solve checkout access.
3. Configure that account's Git identity and only the repository access it needs. A public clone or a deliberately scoped agent credential is separate from the broker's GitHub login and environment-secret rights. Never copy the broker's or human's credential store to the agent. Agent Git/GitHub access must not grant environment-secret management on the target repository: otherwise it could bypass the broker. Prefer public clone or narrowly scoped source-content access; a broad repository credential is not an acceptable substitute.
4. Install one chosen coding-agent CLI using its official instructions, and perform its authentication privately as that account. Do not build a Claude/Codex installer or require both products to complete this milestone. Record which CLI/version was actually exercised.
5. Generate an agent authentication key in the agent account through a human-controlled setup; give the installer only its public key. Configure the generated broker alias and consumer-project AGENTS instructions for that account.
6. Prefer a separate console/desktop login under the agent account. An already-configured ordinary SSH login such as `ssh secretops-agent-user@localhost` is another option, but it is distinct from the dedicated broker port, which accepts only the broker account; do not expose a new general SSH listener for convenience. Do not recommend bare `sudo -iu secretops-agent-user claude` as the default: sharing the human controlling TTY can permit terminal-input injection. A sudo launch is allowed only after verifying effective `use_pty`, which gives the child a separate pseudo-terminal; do not assume distro defaults. Check the resulting UID and terminal separation. Human approval/entry always stays in another human login/terminal.

Record effective sudo `use_pty` and the running kernel's `dev.tty.legacy_tiocsti` setting; doctor reports WARN if `use_pty` is disabled/unverified or the sysctl is not 0/unavailable. Do not change global sudo/kernel settings automatically or treat a warning as permission to use the unsafe sudo-launch route. A separate login remains the recommendation; sysctl defaults and optional launch paths are not assumed security evidence.

VM validation must demonstrate the actual chosen agent starts under the intended UID, can use its checkout and permitted Git access, and reaches the broker through the restricted alias. Command-only SSH tests are necessary but do not prove these account/bootstrap instructions are usable.

## Fixed filesystem and release identity

```text
/opt/secretops/                             root:root 0755
  releases/RELEASE_ID/                      root-controlled, no unprivileged writes
    manifest.json                          root:root 0644
    venv/                                  application and locked dependencies
    bin/                                   versioned broker/agent/admin entry points
  current -> releases/RELEASE_ID            root-managed
/etc/secretops/                            root:secretops-broker 0750
  broker.conf                              root:secretops-broker 0640; non-secret settings
  token                                    secretops-broker 0600; token only
  policy.yaml                              root:secretops-broker 0640
  sshd_config, authorized_keys, inventory   root-managed
  ssh_host_*                               dedicated private host keys root 0600
/var/lib/secretops/                        secretops-broker 0700
  plans/, recipes/, audit/                  secretops-broker 0700
/var/lib/secretops-archive/                 root:root 0700
/usr/local/bin/secretops-{broker,agent,admin} root-owned stable launchers
```

All security-relevant parents, symlink destinations and executable trees are checked; root ownership of one file is insufficient. No credential belongs in a release. The fresh installer refuses an unrecognized existing layout instead of migrating the original laptop broker automatically. The root-managed authorized-keys source deliberately differs from the legacy broker-home `.ssh/authorized_keys` example in BROKER_HOST.md.

A release bundle is explicitly identified as Ubuntu 24.04/CPython 3.12/x86-64. Include the application wheel, locked dependency wheels compatible with the target interpreter/glibc/architecture, scripts and `install.sh`. Do not resolve floating dependencies as root at install time. Manifest records version, source commit, plan contract, platform, managed paths, and sha256 hashes. The commit/artifact identity distinguishes builds that still share package version `0.1.0`.

Publish the outer bundle checksum alongside the release. Internal checksums detect mismatches but do not authenticate a publisher; signing is deferred. Installed doctor trusts only the root-protected active release manifest, never a caller-supplied manifest in a writable bundle directory.

Each stable launcher resolves `current` **once** to an installed release path before invoking code. All subsequent wrapper/application paths derive from that fixed release, never from `current` again or from caller environment. This includes the shell `write-recipe` path and agent → broker delegation; do not acquire a Python lock and assume it covered earlier shell execution. Versioned wrappers and their manifest switch together behind `current`. Stable launcher bytes remain unchanged for the first upgrade contract; incompatible launcher updates are refused rather than claimed atomic.

No new global runtime-lock framework is required for this milestone. Manual maintenance explicitly pauses agent activity, waits for broker commands to finish and stops only the dedicated service before changing active application/state. Existing local human entry must also be idle. After pausing callers and stopping the dedicated service, the human runs `pgrep -u secretops-broker` (or an equivalent process inventory): maintenance proceeds only when no processes under that broker UID remain. Treat process-check errors as failure, not evidence of idleness. Inspect/wait for remaining sessions and legitimate helpers rather than blindly killing them; stopping the service alone may leave established sessions alive. Keep new human/agent calls paused throughout the maintenance window; this check is an operator precondition, not a concurrency lock. If commands were interrupted, handle possible unknown outcomes before proceeding; stopping sshd is not proof that every write was cancelled. State/configuration helper writes are atomic and concurrent admin edits are refused/serialized. Do not claim a live backup or arbitrary concurrent release switch produces a consistent snapshot.

## PR 1 — installable bundle and local boundary

Deliver the maintained builder/bundle, `install.sh`, FHS layout, strict Python configuration loader, release-pinned wrappers, dedicated loopback SSH configuration, basic doctor, account/bootstrap quick start and agent-instruction template. PR 1 doctor checks manifest/hashes, ownership and parent/symlink chains, effective dedicated sshd settings/host pin, and reports local TTY-hardening warnings; it explicitly reports missing provider configuration as unconfigured. Actual dropped-UID/group access checks, complete readiness checks and provider probes arrive in PR 2; PR 1 must not claim those passed. This PR can install an **unconfigured** broker and demonstrate its local boundary; provider-ready behavior is not claimed until PR 2 configuration and PR 3 validation.

- Use the system CPython 3.12/venv prerequisite. Stage and verify offline bundle installation, imports and command registration before activation. Record only installer-owned paths, service files and agent authorization for later maintenance/removal.
- `broker.conf` uses one strict allowlisted literal `KEY=VALUE` parser shared by broker, configure and doctor: reject duplicate/unknown keys, shell syntax, interpolation and multiline assignments. The wrapper invokes fixed installed Python code; no shell `source`, `eval`, environment-selected config or silent legacy `broker.env` fallback. Token is read separately only by trusted broker/credential code; unrelated values never reach provider child environments.
- Install a dedicated loopback-only SSH unit/config independent of the host's `ssh.service`/`ssh.socket`. Never start a public listener or disable unrelated SSH services/sockets. Set `AllowUsers secretops-broker` on this dedicated instance so it accepts no general agent-user/human login; doctor checks the effective setting. Put the supplied public key in the root-managed source with forced `command=` and `restrict`; no shell, scp/sftp or forwarding.
- Generate a dedicated service host key and install a protected host pin for the agent alias, including the port. Use strict checking/batch mode; no TOFU `yes`. The private agent key is human-managed and is not part of the bundle or backup.
- Existing human sudo to the broker remains an administrative prerequisite. Give a concrete password-authenticated invocation; do not add agent sudo rights or broad NOPASSWD rules. The agent's effective noninteractive elevation must fail independently of the human's normal sudo membership.
- Ship basic INSTALL.md steps with PR 1 so installation/account setup is reviewable immediately. Do not defer every usable instruction to a later PR.

## PR 2 — safe configuration and short operating procedures

Add only the necessary human helpers: `secretops-admin configure`, `renew-token`, and `doctor` (including explicit `--providers`). Proposed names are not current commands. Full INSTALL.md and OPERATIONS.md arrive with these helpers; BROKER_HOST.md keeps boundaries/reference rationale and links procedures instead of duplicating them.

`configure` collects non-secret destination settings and a hidden token. `renew-token` replaces only the token, changes no policy/destination, and never uses arguments, an editor, an agent pipe or an old-token backup. Validate a nonblank single-line token, use an exclusive temporary file in the protected directory, set owner/0600, flush and atomically replace. A failed post-replacement check must say the new token was installed but verification failed; no invented rollback. Automatic exchange/renewal from stored Universal Auth credentials is separate work.

Default doctor is local-only: fixed PASS/FAIL/WARN messages and safe remediation, never token/config contents, raw provider output or exception dumps. Verify account topology, actual access by dropping to the agent UID **with its supplementary groups**, executable/parent ownership, wrapper hashes against the protected manifest, pinned configuration paths, timeout, token-file presence/mode without reading its contents, effective sshd restrictions (including `AllowUsers secretops-broker`), host pin, and the TTY-hardening observations above. Mode-bit comparisons alone miss ACL/group access. The supported primary topology fails if the agent can noninteractively elevate; shared UID receives an explicit unsupported-isolation result, not an implied pass.

Optional `doctor --providers` runs curated checks as the broker user, preserving stored-login and child-environment isolation. For Infisical, require a **known existing synthetic probe** in the intended test project/folder/environment; missing is not readiness. For GitHub, suppress authentication diagnostics and report only the safe result; metadata name checks do not establish value equality. WARN on non-loopback HTTP without displaying sensitive URL content. Provider checks are explicit human operations, not added agent permissions, and never write or list raw secrets.

Keep each guide procedural and short, with actor/host labels and links to reference details. Infisical installation is a prerequisite: link official Cloud/self-hosted and identity documentation, state tested versions/minimal rights, and avoid maintaining a competing deployment guide. Start human broker-user `gh auth login` from `cd /`; authentication/device codes stay outside agent output. Update AGENTS.md/AGENT_USAGE.md to explicitly forbid direct reads of `/etc/secretops/token`, protected `broker.conf`, broker login state and private state paths; public metadata commands are the only agent interface to that information.

### Maintenance procedures, not a new administration framework

- **Upgrade:** pause users/agent, wait for active calls, stop the dedicated service, take a protected credential-free state backup, verify/stage a new compatible bundle, switch to the release-pinned wrappers/application and run doctor. Preserve credentials/policy/state. Retain the old release. If validation fails, report the active release truthfully and provide a tested manual command/procedure to restore the previous pointer. No automatic rollback or new rollback subcommand is required; wrappers/manifest must move with the release, never rewind plans/audit, and never silently migrate approvals.
- **Backup:** archive exactly strict non-secret `broker.conf`, policy, and managed plans/audit after quiescence. Use `/var/lib/secretops-archive/upgrade-RELEASE_ID/` or another root-only history directory, archive root-owned 0600. Exclude token, GitHub login/keyring, SSH private/host keys, recipes/import sources and raw value files. Allowlist regular-file/directory paths and refuse symlinks/special files. Full plan/audit record schema scanning is deferred: this private state archive is not a sanitized export for agents/public sharing. Do not discard history to make a backup pass.
- **Restore:** stop activity; verify archive identity/version/checksums/paths/types/size and extract safely into protected staging, never blind root `tar` extraction into live paths. Preserve newer state/history before explicit replacement; do not invent a merge. Put all restored plans into root-owned historical `archive/` (files 0400/directories 0500), never active plans; restore audit as separately identified history. Replan every pending operation and obtain fresh approval. Supply a new Infisical token and repeat human broker-user GitHub login; no credentials are restored. Vault data is external.
- **Unknown outcome:** agent stops on exit 3 and informs the human. `verify_many` proves only name presence. After investigation, the human may approve a new explicit write reasserting the desired vault value; this can overwrite and is not an automatic or universally harmless retry.
- **Uninstall:** human follows the verified inventory, stops/disables only the managed service and removes the managed agent authorization **before** removing its forced-command wrapper. Remove launchers/current application/client references without touching unrelated keys/services. Preserve protected audit/state history; explicitly revoke credentials and remove installed token/login state. Do not orphan retained data or delete the human-managed agent private key implicitly. This is a tested manual procedure, not a required uninstall command.

Guide procedures and fixture checks must cover failure/collision/permission cases. Automated backup/restore/uninstall and broader service management can be added later if actual use justifies them; they are not completion requirements here.

## PR 3 — clean-VM evidence and fixes

Use a fresh Ubuntu 24.04/CPython 3.12 x86-64 VM, not the original laptop and not only a container. Follow only the shipped artifacts/instructions, then record redacted results under `docs/validation/`. PR 3 may include fixes found by this process; PRs 1–2 alone do not establish the completed milestone.

Acceptance checklist:

1. Build/install matching wheel/dependencies/wrappers/manifest from the bundle without developer paths or floating root dependency resolution. Tampered wrappers, wrong platform, writable manifest/parents and unknown existing layouts fail safely. No credentials enter artifacts.
2. Follow the account guide, start the actual chosen coding agent under the separate UID, use its checkout and separately scoped Git access, and connect through the pinned restricted SSH alias. Record agent CLI/version and the exact UID/group topology. Human approval/entry occurs in another terminal. Record the actual launch method, distinct UID/controlling terminal, effective `use_pty` and `dev.tty.legacy_tiocsti`; if using the optional sudo route, demonstrate its separate PTY rather than assume it. A bare shared-TTY sudo launch does not pass this check.
3. Doctor checks access after really dropping UID/groups, not by reading protected contents or comparing modes only. Agent cannot directly read/write configuration/token, broker login state, plans/audit, obtain shell/scp/sftp/forwarding, invoke human/admin endpoints, or approve/reject. Also prove allowed planning/execution works so denials do not pass merely because SSH is broken.
4. Demonstrate `sudo -n -u secretops-broker ...` and alternate noninteractive root elevation fail under the agent account both before and immediately after human sudo, including repeated attempts in the agent terminal. Human credentials/tickets do not cross accounts. Do not claim same-UID isolation or require a second shared-login VM qualification matrix.
5. Human provisions a short-lived identity limited to the separate Infisical test project and logs the broker user into GitHub for the chosen test repository/environment. Provider login/device codes, tokens and approvals never go through agent output. Known synthetic probe passes explicit provider doctor.
6. Complete human `add` → agent saved sync plan/show/preflight → human approval → agent execute → separate `verify_many`. Vault read-back and GitHub acknowledgement have their existing different meanings; name presence does not prove GitHub value equality. Retain current no-output, production-policy, timeout and partial-result tests, including safe post-write storage failures that never claim unsaved state was persisted.
7. Revoke the test token: readiness fails safely with provider error, never missing/session fallback. Human renews via the helper; the known existing probe and workflow recover without changing destination/approval semantics. Token replacement and failure paths preserve ownership/mode and never echo values.
8. Test the documented upgrade, manual rollback, backup, restore and uninstall procedures. Retained plans/audit survive upgrade, restore plans remain non-executable history, and uninstall removes authorization while retaining private audit history. Use two compatible bundles built through the maintained builder; do not require legacy laptop migration or undocumented helpers.
9. Archive traversal/symlinks/special entries/unsafe permissions are refused; credential/login/import paths are absent from the backup. No claims of content sanitization. Config parsing never evaluates shell text.
10. Record OS/image, CPython/wheel compatibility, client/broker/provider versions, bundle checksums, terminal support and each tested procedure/remaining limit. Fix installation blockers. Revoke test identity/token and remove temporary GitHub access/login afterward; signing, human SSH and tmux remain deferred.

## Delivery estimate

PR 1: bundle/install/boundary plus usable account bootstrap; PR 2: configuration/renewal and complete short procedures; PR 3: actual clean-VM validation and fixes. Each PR has its own tests and an honest supported/unconfigured status. No claim that documentation alone or fake providers finish the work.

Estimate: 5–8 working days with VM and test accesses ready. Only the primary separate-account topology is qualified. Review implementation details of stable release resolution, literal config grammar and safe manual archive operations before coding; no general sudo-policy editor, runtime-lock framework or restore engine is included. Drafting this design does not change the existing broker, create a VM, provision credentials or perform provider operations.
