# Onboarding follow-up: v0.1.10

Date: 2026-11-15
System: Ubuntu 22.04 laptop, system Python 3.12, Infisical CLI 0.44.2, existing self-hosted vault, separate agent account.
Status: broker upgraded, local policy updated and live vault connection check passed; fresh-agent handoff test is pending.

Record results and remarks from this run here. Keep credentials, values and private deployment identifiers out of this file. Earlier installation evidence: [first installation](onboarding-first-install.md).

## Results

| Step | Evidence | Result |
| --- | --- | --- |
| Terminal separation | Human-provided `sudo -l` output lists `use_pty`. | The documented `sudo -iu agent` route can be used. |
| Release preparation | Agent downloaded the public v0.1.10 installer, checked its SHA-256 against release notes, validated archive paths/types and extracted it. | Installer prepared before the human ran it. |
| Upgrade from v0.1.7 | Human ran the installer with `--upgrade`; output reports v0.1.10 and preserved configuration, policy, plans, audit, SSH service and keys. Agent independently read installed package metadata. | Upgrade succeeded; installed version is 0.1.10. |
| Policy update | Human copied the existing policy to `policy.before-v0.1.10.yaml`, then installed the agent-prepared replacement. Agent compared the installed file with the prepared file and loaded it through v0.1.10's policy validator. | Only `prod` was added to the allowed environments of `vault.read.check`, `vault.secret.diff_envs` and `github.env_secret.verify_many`. Other policy fields were preserved. Checks confirmed production write denial. No provider operation was used for this validation. |
| Valid vault configuration | Human ran `sudo secretops-configure --check` for `dev`. | `OK vault connection works: URL, token, project and environment dev answered.` Nothing was written; provider output and secret values were suppressed. Folder existence is not established by this check, as its output explicitly states. |

## Remarks and follow-up proposals

- The human asked why a separate manual policy backup was necessary and whether an important step like this belongs in scripts. The backup provides recovery before a manual policy change; it is separate from the upgrade, which deliberately preserves custom policy. If a policy-update helper is introduced, it should include backup, validation and safe replacement rather than require the human to assemble these steps. Do not replace custom policy with packaged defaults automatically. No such helper was implemented in this run.
- The human asked where to find step-by-step documentation. Upgrade, agent setup and validation instructions are still spread across OPERATIONS, INSTALL, AGENT_USAGE and TODO. The guided onboarding entry and role/host outline remain the follow-up tracked in TODO section 6.
- Existing project instructions must never be overwritten by default. The human explicitly rejected replacement of an existing `AGENTS.md` and found the explanation of how the template fits into a real repository unclear. The `install -D` command used in this run was intended only for the new, empty test directory; it can overwrite a destination file and must not be presented as the general integration command. For an existing repository, inspect its applicable instructions, show a proposed diff adding the configured Secret operations section, and preserve unrelated content. If the section already exists, update that section through a reviewed diff without duplicating it; identify conflicting instructions rather than silently choosing or rewriting them. Creating a new file must check that it does not already exist. Include an existing `AGENTS.md` case in onboarding validation, not only an empty directory.

## Fresh-agent test preparation

- The agent extracted the consumer template from the v0.1.10 release, substituting only `ssh BROKER_ALIAS` with `secretops-local` and `HUMAN_BROKER` with the local human broker command. The human reports installing it as `AGENTS.md` in the new test directory under the separate agent account. Launching a fresh coding-agent session and validating its handoff remain pending.

## Next checks

- Replan and approve any still-needed operations whose approvals predate this upgrade; do not replay executed plans.
- Test a fresh coding-agent session with only the current consumer-project instructions, without corrective prompts (TODO 4a).
- Validate failure classification with deliberately wrong URL, revoked test token, wrong project, environment and folder (TODO 4f); use disposable test credentials and restore valid configuration afterwards.
- First real-project use in dev/staging, with friction recorded.
