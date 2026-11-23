# secretops goals and roadmap

This document records product goals and evidence of progress. [TODO.md](../TODO.md) tracks implementation tasks in dependency order. The horizons below describe progression, not delivery dates or existing capabilities.

## Direction

Agents should be able to carry out explicitly authorized credential workflows while a trusted broker holds credentials, enforces policy, and keeps secret values outside the agent's view. Humans decide permissions and destinations rather than manually moving credentials between systems.

A runtime credential proxy lets an agent *use* a secret; secretops lets an agent *manage* secrets: enter, generate, rotate, copy, and sync them through saved plans, human approval, and an audit log. It complements proxies and vaults and replaces neither.

Over time the human moves from approving each operation to setting the boundaries: policy and limited grants define what an agent may do alone, and anything outside them becomes an approval request with a record.

External vaults remain the source of truth. Keep the product focused on a small number of useful workflows and verifiable security boundaries.

## Short term — proven in real use, then announced

The first complete workflow is reached and released: a human enters an externally created API key through a hidden prompt, the broker stores it in Infisical, and an agent transfers it to GitHub through a separately approved plan. It is repeatable from a clean installation ([entry](validation/interactive-secret-entry.md), [GitHub sync](validation/github-target-reliability.md), [installation](validation/local-broker-installation.md), [first install by a new user](validation/onboarding-first-install.md)).

Next:

- Use the published release for a real project and record where it is hard. Fix what the record shows before adding features.
- Confirm the two parts that were only tested in isolation: a fresh agent hands a plan to the human correctly from the documented instructions alone, and the vault connection check reports real provider failures correctly.
- Lower the cost of a first try: a short demo recording and a trial that needs no root and no existing vault.
- Announce once, with an accurate description of tested guarantees and limits.

Success means: the owner's real project uses secretops for its secret operations, and a new user reaches a first approved operation from the documentation alone.

## Medium term — independently usable and reliable

### External users can succeed without our assistance

Validate installation, secret entry, approval, and GitHub sync with initial external users. Use observed difficulties to choose improvements rather than expanding the feature list in advance.

Evidence: people outside the original development setup complete the documented workflow; recurring setup and operating problems become reproducible issues and fixes.

### The broker is practical to operate every day

Provide supported credential renewal, upgrades that preserve state, useful audit records, documented recovery from interrupted operations, and several vault projects per broker. Make known provider limitations and unresolved outcomes visible without exposing values.

Evidence: routine expiry, upgrade, and failure/recovery scenarios can be handled through documented procedures rather than ad hoc intervention.

### Policy distinguishes secrets by name

Add secret-name patterns as a policy dimension beside action and environment ([design](design/name-pattern-policy.md)), so that low-risk names can be handled differently from sensitive ones. This is the base for grants.

Evidence: the same action in the same environment is allowed, sent to approval, or denied according to the name, and the decision is visible in the plan before execution.

### Approval does not need the broker's terminal

Let the human review and decide a plan outside the shell, in stages; each stage is useful on its own, and the terminal path stays:

1. **Notification only.** The broker tells the human that a plan waits; the decision stays in the terminal. Done in 0.1.15 with ntfy ([how](OPERATIONS.md#notify-the-human-when-a-plan-waits)).
2. **A page with a passkey.** A page served on the broker host lists waiting plans with the full review; the human decides with a passkey (WebAuthn with user verification), also from a phone over a private network. The passkey lives on the human's own device (a phone or a hardware key), not on the broker host, so an agent on the same host cannot use it; the page accepts no decision without a fresh WebAuthn assertion.
3. **GitHub as a channel** (experiment). The plan becomes a deployment that waits for a required reviewer, so the GitHub mobile app notifies and records the decision; the broker accepts it only when it is bound to the plan digest. This ties approval to GitHub.
4. **Bank-style app.** A key on the human's device signs the plan digest. Only if the stages above fall short.

In every stage the decision is authenticated by something the agent cannot reach, shows exactly what is approved, and is bound to the plan content like a terminal approval; notifications carry no secret names. Push approval invites approving without reading, so fewer and clearer requests (name patterns, grants) come first.

Later, four-eyes approval: with one passkey per person, a plan can require two different approvers, and the audit names both.

Evidence: a plan is approved or rejected without a shell session; an agent on the same host cannot produce or replay a decision; the audit records which channel was used.

### Agent access expires

Replace the agent's permanent restricted key with an SSH certificate valid for a few hours, so access ends on its own without removing a key. This needs no change to secretops: a CA line with the same forced command and `restrict` in the broker user's `authorized_keys`, and a human who signs each certificate with a CA key the agent cannot reach.

Evidence: an expired certificate is refused; a valid one reaches only the wrapper allowlist; the agent cannot sign one.

### A second vault proves provider independence

Trial OpenBao operationally, then add a narrow adapter with explicit conditional-write capabilities. Preserve Infisical and document differences in guarantees instead of treating weaker semantics as equivalent. Motivation beyond independence: a team vault without Infisical's cost. Target deployment: a compose file (nerdctl or Docker) and Kubernetes (Helm); backup through integrated-storage snapshots.

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
