# secretops TODO

Product direction and short-, medium-, and long-term goals are recorded in [docs/ROADMAP.md](docs/ROADMAP.md). This file tracks concrete work and implementation order.

## Current status and short-term goal

secretops is a CLI-first SecretOps broker. It is not a vault: external vaults remain the source of truth, and agents operate on names, metadata, plans, approvals, and safe results rather than secret values.

The Infisical adapter hardening is implemented and verified against the NAS instance. The dedicated local SSH broker separates agent access from vault credentials and human approval.

**Short-term goal:** a human can enter an externally created API key through a hidden prompt, the broker stores it in Infisical, and an agent can transfer it to GitHub through a separately approved plan. The workflow must be documented, repeatable from a clean installation, and require no undocumented helpers from `/tmp`.

Plan destination binding is complete and validated (PR #6). The human secret-entry CLI (`secretops add`) is implemented, with direct and agent-prepared entry validated through the real local sudo broker ([results and limits](docs/validation/interactive-secret-entry.md)). GitHub target reliability and the complete local-sudo entry-to-GitHub workflow are now validated ([results and limits](docs/validation/github-target-reliability.md)). Installation and operating guides are merged (PR #10) and a clean installation is validated with a self-hosted Infisical and GitHub ([results and limits](docs/validation/local-broker-installation.md)). Remaining manual checks are tracked below. The public repository and [v0.1.0](https://github.com/feistify/secretops/releases/tag/v0.1.0) are released. Next: first real-project use (section 6), then several vault projects per broker (section 7).

## P1 — Deliver the first complete daily-use workflow

### 1. Human CLI for secure secret entry — highest product priority

- [x] Add interactive entry of an externally created credential, such as a Scaleway IAM API key, without manually preparing an import file. Start with one name and one value. Implemented as `secretops add` and `vault.secret.add` ([design and gate results](docs/design/interactive-secret-entry.md)); local-sudo live results are recorded [here](docs/validation/interactive-secret-entry.md).
- [ ] Finish real-broker validation (design acceptance criteria 11–14). CLI `0.43.99`, direct and agent-prepared entry, read-back confirmation, and existing-name refusal passed through local sudo. Standalone terminal paste/cancel/decline checks passed. Still open: complete broker cancellation/decline checks, changed destination, live agent-endpoint denial, audit validation, terminal application/version identification, and the SSH routes if supported. Keep partial evidence and remaining limits in the [validation record](docs/validation/interactive-secret-entry.md).
- [ ] Before claiming these combinations are validated, test human secret entry over SSH (direct and remote sudo) and terminal input inside tmux. Both remain untested; current manual entry evidence covers local sudo without tmux only.
- Prompt for name, environment, and explicit destination metadata. Use a hidden terminal prompt for the value; refuse echoed fallback when no suitable terminal is available.
- Transfer the value through a narrowly scoped human-to-broker channel, such as stdin over SSH. Keep it out of arguments, shell history, recipes, plans, output, audit logs, and agent-visible files. Credentials remain on the broker.
- Preserve policy checks, saved planning, preflight, human approval, and create-only defaults. Replacement is explicit. Design how submission is tied to the approved plan and destination before implementation; collect the value only when needed for execution.
- Let the agent orchestrate metadata and subsequent approved operations. Human entry happens outside agent tool output; do not add an unrestricted input command to the agent SSH allowlist.
- Reuse confirmed/unknown vault write outcomes and never automatically retry an unconfirmed write. Any provider-required temporary files remain broker-private and are cleaned up.
- Test cancellation, denied approval, existing target, provider failure, and the no-secret-output invariant.
- Scope: import an existing credential. Automatic provider credential creation is a separate integration.

### 2. GitHub target reliability and workflow validation

Design: [GitHub target reliability](docs/design/github-target-reliability.md), merged into `main` in PR #9. The complete local-sudo workflow passed on `d0d17aa` ([live validation and limits](docs/validation/github-target-reliability.md)). The design retains plan contract version 1 and aligns unknown-write exit code 3 across GitHub and vault execution, including partial writes.

- [x] Give `gh` an explicit allowlist of required environment variables instead of inheriting the broker environment. Never pass `INFISICAL_TOKEN` or other unrelated broker credentials; preserve the intended GitHub authentication and trusted connection settings. Add regression tests for both secret writes and metadata verification. Real-broker results and evidence limits: [GitHub environment isolation validation](docs/validation/github-environment-isolation.md).
- [x] Add bounded timeouts to GitHub target calls and safe errors for launch, authentication, and availability failures.
- [x] Distinguish failures before a write starts from unconfirmed outcomes after launch. Do not claim a target was unchanged after a timeout, and do not automatically retry. Unknown outcomes exit 3 for GitHub and vault writes.
- [x] Preserve useful partial results for multi-secret sync in safe output and audit records. Implemented per the [design](docs/design/github-target-reliability.md); automated tests only.
- [x] Validate the complete human-entry → broker → Infisical → separately approved GitHub sync workflow on the current broker with synthetic dev/staging values. Passed through local sudo on `d0d17aa`: human entry/read-back, approved sync, metadata verification, and human UI name-presence check. [Evidence and remaining limits](docs/validation/github-target-reliability.md); SSH entry and tmux remain untested.
- Define success according to the target's capabilities. GitHub metadata presence alone does not prove byte-exact equality with the intended value; do not promise secret read-back where it is unavailable.

### 3. secretops installation and operating guides

Scope: [repeat the existing broker setup](docs/design/local-broker-installation.md) in one PR: installer for the current layout, small safe configuration/renewal helper, INSTALL.md, OPERATIONS.md and clean-VM evidence. Existing SSH is a prerequisite; no new deployment architecture or shared-account isolation claim. The [larger managed-installation design](docs/design/local-broker-installation-later.md) is deferred reference.

Merged in PR #10: `scripts/install.sh` (install and `--upgrade`), `secretops-configure` (strict `broker.env` writer, hidden token prompt, `--token-only` renewal), `secretops-local`, [INSTALL.md](docs/INSTALL.md) and [OPERATIONS.md](docs/OPERATIONS.md). Installer checks pass in a clean Ubuntu 24.04 container (`tests/test_install_container.sh`). The [clean-VM validation](docs/validation/local-broker-installation.md) ran the first workflow with a self-hosted Infisical and GitHub; it found an sshd start race (fixed in PR #10) and a refused re-execution overwriting the status of an executed plan (fixed in PR #11).

- [x] Write supported installation and daily-use guides, linked from the README; ship with the entry CLI and before public v0.1. Clearly label who runs each command and on which host.
- Document installing, verifying, upgrading, and uninstalling secretops itself; include Python >=3.11 and an appropriate package installation path, broker/agent entry points, configuration and state locations, and preservation of credentials, plans, and audit during upgrades.
- Explain the vault prerequisite: use an existing Infisical Cloud account/region or install a self-hosted instance. Installing secretops does not install Infisical. Verify both paths against supported versions and link official provider instructions.
- Cover the tested Infisical CLI version, explicit server/project/folder, machine identity permissions, token delivery and expiry, dedicated broker account, trusted executables, restricted SSH access, and human approval. Include GitHub authentication and target permissions for sync.
- Cover entry/import, generation, rotation, environment copy, GitHub sync and metadata verification, recipes, and plan/show/preflight/approve-or-reject/execute. Keep vault rotation and target sync separate.
- Document the supported dotenv subset from the existing parser, including quoting and escape behavior; do not imply full shell or dotenv expansion support.
- Include synthetic first-use and smoke examples, safe troubleshooting, missing versus provider error, unknown outcomes and manual verification before retry, cleanup, and agent safety rules. Mark planned commands unavailable until implemented.
- For self-hosting, cover persistence, backup/recovery, HTTPS, and when account flows need SMTP. NAS notification settings do not configure Infisical email.
- [x] Verify the guide in a clean setup. Put required maintained helpers in the repository/package rather than depending on `/tmp` scripts.
- [ ] Not yet covered by the guides: Infisical Cloud (only self-hosted over HTTP was validated), self-hosting notes (persistence, backup, HTTPS, SMTP), the dotenv subset for import, and worked examples for generation, rotation and environment copy. Upgrade, backup/restore and token renewal were checked in the container only.

### Deferred installation tooling

- [ ] Install from the published release instead of a local build: `install.sh` fetches the exact version from PyPI (or the GitHub release) and checks its hash, so `uv` and a build step are no longer needed. Until then, announcements present PyPI as provenance, not as an installation path: `pip install secretops` alone gives no broker boundary. Decide after the first real-project use shows how much the current installation hurts.
- [ ] Revisit bundle/manifest tooling and fully locked platform-specific dependency distribution when release needs justify it.
- [ ] Consider FHS migration, separate token storage and strict non-shell configuration loading as a separate compatibility change.
- [ ] Consider doctor/admin tooling and a primary separate-agent-account rollout with sudo/TTY hardening; none is required by the minimal installer PR. (The dedicated loopback sshd and pinned host key are part of the existing setup and are installed.)
- [ ] Consider release switching/rollback automation, archive-only restore and automated uninstall after the documented operating path is usable. Keep recovery/uninstall procedures in the broader operational backlog.

### 4. Broker token delivery and renewal

- [x] Provide a documented human-operated renewal path, with expiry and failure handling, before relying on the broker for daily use. `secretops-configure --token-only` and [OPERATIONS.md](docs/OPERATIONS.md#renew-the-vault-token); a live renewal against a real vault is not yet recorded.
- [ ] Design automated short-lived token delivery separately, such as exchanging Universal Auth client credentials at invocation time. Keep bootstrap credentials broker-only; no interactive login fallback or credential output.
- Automatic renewal is a follow-up to the first workflow, not a reason to broaden the secret-entry adapter.

### 5. Public GitHub repository preparation

- [x] Prepare a small public early-stage project after the guides and complete workflow are usable. OpenBao and additional integrations are not prerequisites.
- [x] Review the published files for credentials and private deployment data before publication. Tracked files and built packages were reviewed with Gitleaks; the private history, Actions logs and artifacts were not published, so they were not part of the review.
- [x] Choose an open-source license and add `SECURITY.md` with a private vulnerability reporting channel and supported-version policy. Apache-2.0; GitHub private vulnerability reporting must be enabled in the repository settings when the repository becomes public.
- [x] Replace the placeholder package description and review release metadata. README describes intended users, supported capabilities, broker boundary, tested versions and limitations, with a synthetic entry-to-GitHub demo. Package metadata marks the project alpha and links source, installation docs and issues; publication remains a separate step.
- [x] Publish to a new public repository with fresh history, keeping the existing repository private. The public repository is `feistify/secretops` (organization `feistify`); package URLs point to it. Tracked files were reviewed with Gitleaks before publication; the local `.infisical.json` is no longer tracked.
- [x] Review the publication checklist with the owner; change visibility only with explicit authorization. The owner reviewed and published `feistify/secretops` and enabled private vulnerability reporting.
- [x] Prepare the public v0.1 artifact/release and verify its documented installation path. Released as [v0.1.0](https://github.com/feistify/secretops/releases/tag/v0.1.0) with the wheel and source distribution built from the tag; the installer was checked in a clean container before tagging. Public source availability is separate from production-readiness claims; production execution remains restricted by policy.
- Keep the initial scope to a useful repository and early-user feedback. A marketing site is not a prerequisite.
- [x] Publish v0.1.0 to PyPI: the same files as the GitHub release, so the hashes match. [Published](https://pypi.org/project/secretops/0.1.0/) by `publish.yml` through trusted publishing, after approval in the `pypi` environment; no PyPI token is stored.
- [x] Protect `main` with a ruleset: no force pushes or deletion; consider requiring CI before merge. Rulesets: `main` changes only through pull requests, without force pushes or deletion; release tags `v*` cannot be moved or deleted. CI is not required, and there is no bypass list.
- [ ] Make README links work on PyPI. PyPI resolves relative links against `pypi.org/project/secretops/`, so the v0.1.0 project page has broken links to `docs/`, `SECURITY.md`, `TODO.md` and `LICENSE`. README now uses absolute links to `main` on GitHub (`uv_build` cannot rewrite them at build time). Done when v0.1.1 is on PyPI: a PyPI version can never be re-uploaded, so v0.1.0 keeps the broken links.
- [x] Skip CI for documentation-only changes (`paths-ignore`): Markdown, `LICENSE` and `NOTICE`. If CI becomes a required check, docs-only pull requests need a solution first.

### 6. First real-project use — before the next feature

The clean-VM validation proved the workflow with synthetic values. Next, use the published version for a real project (for example ExposeGPU in dev/staging) and record where it is hard.

- [ ] A human enters an existing API key with `secretops add`.
- [ ] The agent prepares a GitHub sync plan; the human reviews and approves it; the agent executes it and verifies the name.
- [ ] The project uses the secret, and its agent instructions say how to work through the broker from now on.
- [ ] Record where it is hard: installation, finding commands, approval, messages or maintenance. Choose one small UX fix from that record.
- [ ] Ideally, another person follows the installation guide without help.

If one project per broker blocks this work, named destinations (section 7) come first.

### 7. Several vault projects per broker

Today a broker serves one Infisical server, project and folder, set by the human in `broker.env`. With one project per application, that means switching the destination with `secretops-configure`.

- [ ] Short [design](docs/design/named-vault-destinations.md) first: a human-maintained list of named destinations (name → project and folder; same server and token), a recipe field and a `secretops add` option that select a name from that list, and unchanged behavior when only one destination exists.
- Agents choose only among names the human configured; plans keep recording the full destination and approval stays bound to it.
- Not in the first version: different servers or tokens per destination, per-destination policy, several brokers on one machine.
- Implement with tests, then a short VM validation with two projects.

## Architecture and test requirements — apply to upcoming changes

### Preserve the option to change implementation language

- [ ] Document the externally observable contracts independently of Python: recipe and saved-plan formats, approval state transitions, audit events, CLI exit/status behavior, adapter errors, and write outcomes. Define compatibility/version handling where persisted formats change.
- [ ] Keep CLI presentation, policy and lifecycle rules, provider adapters, and broker transport separated through narrow interfaces. Extract boundaries as needed for real work rather than introducing a generic framework.
- [ ] Build reusable contract fixtures and acceptance scenarios in neutral formats such as JSON/YAML, with synthetic values only. Make key scenarios runnable against a supplied executable, not solely through Python imports or internal monkeypatches.
- [ ] Retain Python unit tests for implementation detail. Add process-level coverage for policy denial, approval enforcement, missing/empty/error distinctions, destination binding, no-secret-output, partial writes, timeouts, and unknown outcomes so a future implementation can prove equivalent behavior.
- [ ] Preserve real-provider/broker acceptance scenarios and tested-version records. Fake providers alone are insufficient; they previously missed a real token-auth CLI write bug.
- A future rewrite must re-establish runtime-specific safeguards such as environment isolation, subprocess handling, temporary-file permissions, and secret-safe exceptions. Translating code or passing format fixtures alone is insufficient.
- Stay on Python now. These requirements support a future migration without scheduling one or maintaining duplicate implementations; npm distribution is a separate packaging choice.

## P2 — Deferred until the daily-use workflow is proven

- [ ] OpenBao operational trial, then a narrow adapter and conditional-write contract (create-only/versioned replace). First confirm actual install, authentication, restart/unseal, backup, and restore behavior. Preserve the Infisical adapter and document its non-atomic existence-check/write limitation.
- [ ] Stronger policy rules for action, environment, target, approval, name patterns, and production. Use the reviewed `docs/design/name-pattern-policy.md` when needed; production enablement requires deliberate controls and tests.
- [ ] Refactor large CLI/lifecycle code into handlers/services where changes justify it, preserving output, plan state, audit, and adapter contracts.
- [ ] Improve brokered metadata verification UX using existing actions; do not require agents to run raw provider listing commands.
- [ ] Policy-gated reads of explicitly non-secret connection metadata; no general secret-reveal surface.
- [ ] Brokered constrained SSH operations using a vault-held key, without exposing the key to the agent.
- [ ] Eventual multi-broker daemon/control plane. The current restricted SSH forced-command broker is already implemented; a daemon is not required for the first workflow.
- [ ] Report the Infisical machine-token `NAME=@file` bug upstream if the owner authorizes a public report.

## Future backlog

- [ ] NAS backup secrets POC and first real backup sync (parked since 2026-07-05).
- [ ] Ephemeral broker packaging for one-time migrations; see `docs/BROKER_HOST.md`.
- [ ] Broker-side Docker secret capture; explicitly account for Docker socket privileges.
- [ ] Provider-specific credential creation followed by vault storage: AWS, Scaleway, Cloudflare, Stripe.
- [ ] Create, renew, and rotate website TLS certificates through approved broker workflows. Define CA/ACME integration, domain validation, broker-private key storage, certificate deployment, expiry monitoring, and verification before retiring the previous certificate/key.
- [ ] Generate and rotate SSH key pairs through approved broker workflows. Keep private keys outside the agent's view; explicitly authorize public-key distribution to target hosts/services, verify replacement access before removing old authorized keys, and define recovery for partial rotation.
- [ ] Jenkins target adapter.
- [ ] Automatic combined workflows with explicit authorization boundaries.
- [ ] Secret versioning/rollback and rotation propagation tracking.

## Completed and validated

### Plan destination binding — completed safety prerequisite

- [x] Store the resolved, non-secret vault destination in saved plans: backend, server, project, folder path, and a contract version covering the fixed read semantics. Never store tokens or client secrets.
- [x] Show that destination in the safe plan view; preflight and execution must reject a connection mismatch before provider calls. Define handling of older plans without a binding; do not silently reuse their approvals for a newly resolved destination.
- [x] Preserve credential-free planning and plan inspection. Changing a token alone must not change the destination identity or put credentials in an artifact.
- [x] Cover configuration changes and unchanged-destination credential renewal with tests. Complete before the new secret-entry workflow and before introducing a second backend.

Implementation and validation: [plan destination binding](docs/design/plan-destination-binding.md), including the real-broker check completed on 2026-10-18.

### Earlier completed work

- [x] Saved plans, safe `show-plan`, preflight, approval lifecycle, and execution state transitions.
- [x] Restricted SSH broker transport with human-only approval at the transport boundary; credentials and plan store stay on the broker.
- [x] Vault generate, rotate, import-file, copy-many, and metadata-only environment comparison workflows.
- [x] Import-file policy-before-parse checks, safe parser errors, saved-name consistency, and permission checks (PR #2 and follow-up fixes).
- [x] Partial import/copy audit reporting with confirmed and not-written names, unknown-outcome handling, and regression tests.
- [x] Infisical adapter hardening, including isolated environment, explicit configuration, typed errors, timeouts, YAML file writes, and exact internal write read-back.
- [x] NAS acceptance on 2026-10-17: CLI v0.43.99, server v0.161.10; results and limits in `docs/design/infisical-adapter-hardening.md`. Generate/rotate/copy, literal references, excluded links, negative auth/availability checks, and 38 audit metadata records passed. The personal-override flag and stored-session fallback were not independently isolated in real-instance tests.
- [x] GitHub environment write, explicit multi-secret sync, and metadata-only verification; historical dummy/dev and BACKUP_* staging smoke proofs are described in `README.md`.
- [x] Production-denial smoke proof: approved production sync rejected before provider calls; production writes remain future work.
- [x] Tests cover absence of synthetic secret values from stdout/stderr, audit, plans, and command arguments.
- [x] Threat model and broker deployment documentation in `docs/SECURITY_MODEL.md`, `docs/BROKER_HOST.md`, and `docs/AGENT_USAGE.md`; expand into the complete guides above.

## Safety invariants

- Never print or log secret values, place them in plans/audit, or pass them in command-line arguments. Plans and preflight do not generate values or mutate secrets.
- Use stdin or narrowly controlled provider input; provider-required temporary files are restricted and broker-private.
- Treat provider errors as errors, not missing secrets. Report unconfirmed writes honestly; never automatically retry or roll back partial writes.
- Generate is create-only by default; rotate explicitly replaces an existing vault secret. Infisical currently checks then writes without an atomic create-only guarantee.
- Rotation and target sync remain separately authorized operations. GitHub metadata verification does not read secret values.
- Agents must not read `.env`, tokens, keys, machine identity secrets, or private credential files, nor invoke provider commands to print/list/dump/export secrets. Follow `AGENTS.md` and broker command restrictions.
