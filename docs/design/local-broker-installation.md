# Design: repeat the existing local broker setup

Status: active minimal scope agreed in discussion; implementation belongs to the implementing agent, followed by review. No installation code is added by this document.

## Goal and scope

Make the existing broker setup reproducible from the repository: one installer, safe configuration helper, two short guides and one clean-VM check. Keep the current application, CLI, wrappers and `/opt/secretops` layout. One installation PR; no deployment architecture redesign. The [larger managed-installation proposal](local-broker-installation-later.md) is deferred reference only.

## Deliverables

1. **`scripts/install.sh`:** create the dedicated broker user, administrator-owned venv/application and existing matching wrappers; create private broker-owned state/configuration paths; register an explicitly supplied agent public key with forced `command=` and `restrict`. Existing SSH service/configuration is a prerequisite: do not open public ports, enable a new listener or rewrite global SSH configuration automatically. Document the local connection and human-verified host key. Refuse unknown existing layouts; never silently overwrite configuration, credentials or state. Upgrade requires explicit `--upgrade` or a separate documented procedure, stages the new application before switching, and retains the previous application for recovery.
2. **Small configuration helper:** human enters the machine token in a hidden prompt, outside the agent session. Ship it in the repo; no dependency on an undocumented `/tmp` helper. No token arguments, echoed input, credential output, editor-based instructions or automatic provider login. Preserve the current adapter authentication contract. Root installer never sources broker-owned shell configuration. Safely serialize managed assignments, preserve owner/0600, update atomically and refuse unsupported input without displaying it. Renewing the token preserves destination and other settings; no new admin framework.
3. **`docs/INSTALL.md`:** state the one tested Ubuntu VM version and Python/provider prerequisites, link official Infisical Cloud/self-hosted setup, explain minimal machine-identity permissions, install/configure, broker-user GitHub login, agent public-key/SSH setup, human client configuration and first synthetic workflow. Labels identify actor and host; no credentials are supplied through the agent.
4. **`docs/OPERATIONS.md`:** daily `add` → saved sync plan/show/preflight → human approval → execute → separate `verify_many`; exit 3 and partial outcomes; safe token renewal; explicit upgrade; protected backup of plans/audit/policy. Exclude the entire `broker.env`, GitHub login, keys and import files from backup; document re-entering deployment settings/credentials. A metadata check proves name presence, not value equality. No automatic retry of unknown writes.

Application, wrappers, policy and security-relevant parent directories are administrator-owned and not writable by broker/agent. Only broker configuration, home/login and state belong to the broker with restrictive permissions. Preserve existing policy; do not silently enable production or expand permissions. Use a wheel and matching wrappers from the same source revision; do not require a published PyPI release or new release-bundle system to test this PR.

## Honest boundaries

The agent's restricted SSH channel permits metadata planning and human-approved execution. Human entry/approval uses separate human access. If human and agent share a Unix account, a broker password prompt or instruction file is a convention, not isolation from that agent: it may reach the human's credentials, sudo authority or terminal. Document this explicitly; do not claim the installer fixes it. Human credential entry and sudo stay outside the agent session. No primary separate-agent-account rollout or shared-account hardening framework is required in this PR.

A clean VM is still required to prove account permissions and actual SSH behavior; fake providers alone do not complete validation. Use the existing SSH prerequisite without modifying unrelated services. Stop activity before manual upgrade/backup and preserve audit history; no claim of consistent snapshots during writes. Prior laptop validation is evidence for the workflow, not a substitute for a fresh installation.

## Acceptance and completion

- From a clean VM, a human follows only repository instructions and shipped helpers, with no developer-specific paths or hidden `/tmp` dependencies. Record OS, Python, provider/application versions and exact topology/access route.
- Installer tests cover ownership/modes, unknown-layout refusal and preservation of configuration/state on explicit upgrade. Matching wrappers are installed; no arbitrary shell/approval through the agent key and no raw credential access through that SSH channel. Test allowed commands too so a broken connection cannot count as isolation.
- Human uses a scoped test Infisical identity and test GitHub repository/environment, configures credentials privately, and completes synthetic entry → separately approved sync → name verification. Record the difference between vault read-back and GitHub acknowledgement.
- Renew the token through the shipped helper, demonstrate safe rejection with an invalid/revoked test token and recovery with a valid token, with no login fallback or credential output. Do not deliberately create ambiguous live writes to test timeouts.
- Exercise the documented upgrade and backup procedure; backup excludes credentials and retains plans/audit/policy. Test diagnostics never expose values. Existing Python and wrapper checks pass.
- Write results and remaining limits under `docs/validation/`, fix setup blockers and revoke temporary test access. README links the guides. Document unverified combinations rather than claim universal platform/SSH/terminal support.

Completion for this PR means those four deliverables and the clean-VM evidence are usable together. Automated restore/uninstall, FHS migration, doctor, native bundle/manifest tooling, separate token storage, removal of shell configuration, extra sshd instances, account/TTY hardening and a release-switch framework are deferred. Broader operational/public-release readiness remains tracked separately; do not mark it complete merely because this installer works.

Estimate: 2–3 working days with a VM, existing SSH and test accesses ready, subject to fixing problems found during the clean install. The current broker is not changed while implementing/testing this PR.

## Implementation notes

- The existing setup already used a dedicated loopback-only sshd (`secretops-local-sshd.service`, `127.0.0.1:22222`, `AllowUsers secretops-broker`, own host key) rather than the host's SSH service. The installer reproduces it: it is the existing layout, not a new mechanism. It does not change or start the host's `ssh` service; `openssh-server` is a prerequisite.
- Instead of a supplied public key, the installer generates the agent key as the agent user, in that user's `~/.ssh`, with the pinned host key next to it, as the original helper did. Files in the agent's home are written as the agent, never by root following a path the agent controls.
- The application is a root-owned venv at `/opt/secretops/venv` built from the system Python, `scripts/broker-requirements.txt` (hash-pinned, exported from `uv.lock`) and the wheel. The original bundled its own Python runtime; the Ubuntu 24.04 system Python replaces it.
- `secretops-configure` is `secretops.broker_config` in the package, so it is versioned and tested with the application. It accepts only the canonical `KEY=<shlex.quote(value)>` lines it writes, for known keys, so a file it reads means exactly what the shell wrapper will source.
- `tests/test_install_container.sh` runs the real installer in a clean Ubuntu 24.04 container with a fake Infisical CLI: layout and modes, overwrite refusal, agent allowed commands, denial of approval, add endpoints, shell, forwarding, file access and sudo, a changed host key, terminal-only configuration, token renewal reaching the vault CLI without output, and upgrade. It does not replace the clean-VM run with real providers.
