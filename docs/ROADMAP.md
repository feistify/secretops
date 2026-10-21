# secretops goals and roadmap

This document records product goals and evidence of progress. [TODO.md](../TODO.md) tracks implementation tasks in dependency order. The horizons below describe progression, not delivery dates or existing capabilities.

## Direction

Agents should be able to carry out explicitly authorized credential workflows while a trusted broker holds credentials, enforces policy, and keeps secret values outside the agent's view. Humans decide permissions and destinations rather than manually moving credentials between systems.

External vaults remain the source of truth. Keep the product focused on a small number of useful workflows and verifiable security boundaries.

## Short term — one complete daily-use workflow

A human enters an externally created API key through a hidden prompt, the broker stores it in Infisical, and an agent transfers it to GitHub through a separately approved plan.

Success means:

- Saved approvals are bound to the intended vault destination.
- Human input, broker execution, and GitHub sync work without revealing values to the agent, output, plans, audit, or command arguments.
- Failures and unknown outcomes are reported accurately, without automatic retries of unconfirmed writes.
- A clean installation can reproduce the synthetic workflow using maintained installation and operating instructions, without undocumented temporary helpers.

Destination binding is completed and validated; see [the validation record](design/plan-destination-binding.md#manual-validation-result--2026-10-18). Interactive entry and GitHub reliability are implemented, and the complete local-sudo workflow has passed [live validation](validation/github-target-reliability.md). A clean installation from the maintained [installation](INSTALL.md) and [operating](OPERATIONS.md) guides reproduced the workflow on a fresh VM ([record](validation/local-broker-installation.md)). The recorded remaining manual checks are still open. TODO P1 tracks them, broker token handling, and public repository preparation.

## Medium term — independently usable and reliable

### External users can succeed without our assistance

Publish a small, documented early-stage project and validate installation, secret entry, approval, and GitHub sync with initial external users. Use observed difficulties to choose improvements rather than expanding the feature list in advance.

Evidence: people outside the original development setup complete the documented workflow; recurring setup and operating problems become reproducible issues and fixes.

### The broker is practical to operate every day

Provide supported credential renewal, upgrades that preserve state, useful audit records, and documented recovery from interrupted operations. Make known provider limitations and unresolved outcomes visible without exposing values.

Evidence: routine expiry, upgrade, and failure/recovery scenarios can be handled through documented procedures rather than ad hoc intervention.

### A second vault proves provider independence

Trial OpenBao operationally, then add a narrow adapter with explicit conditional-write capabilities. Preserve Infisical and document differences in guarantees instead of treating weaker semantics as equivalent.

Evidence: the relevant lifecycle and security contract scenarios pass against both backends, with supported capabilities and limitations documented. Selection of the backend does not require changing workflow orchestration.

## Long term — bounded agent autonomy across the credential lifecycle

### The agent orchestrates creation through revocation

For concrete supported provider workflows, an agent requests credential creation, and the broker uses a previously provisioned identity to create the credential, store it in the vault, distribute it, rotate it, and eventually revoke it. Secret values never return to the agent. Initial provider trust and permissions still require deliberate human setup.

Evidence: a selected real workflow completes with recorded authorization and safe audit results; failures and partial completion remain visible and recoverable.

### Humans authorize boundaries for repeatable operations

Introduce explicit, limited grants for recurring operations, scoped by agent, action, secret names, destinations, expiry, and where needed execution count. Retain per-plan approval where appropriate and stricter production controls. Vault token lifetimes do not replace operation-level authorization.

Evidence: allowed repeated work proceeds within the grant, while expired, revoked, exhausted, or out-of-scope requests are denied before mutation.

### Rotation tracks downstream adoption

Record which destinations depend on a credential and which have adopted its replacement. Distinguish confirmed delivery, failure, and unknown outcomes. Define provider-specific verification and safe revocation conditions; successful delivery alone does not prove that a consumer has switched credentials.

Evidence: an operator can see remaining rotation work and decide when an old credential can safely be revoked, without revealing either value.

## Constraints on growth

- Add integrations in response to demonstrated workflows; a target number of providers is not a goal.
- A central service or multi-broker control plane is optional architecture, not a prerequisite.
- Preserve narrow interfaces, documented formats, and reusable acceptance scenarios so a future language change can establish equivalent behavior. Remain on Python until a concrete need justifies migration.
- Public availability is separate from production readiness. Describe tested guarantees and limitations accurately at each stage.
- Revisit priorities using real usage and maintenance cost; these goals do not authorize provider integrations or relax approval and secret-handling rules.
