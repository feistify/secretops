# Design: interactive secret entry

Status: implemented and merged into `main` in PR #8; review fixes and partial local-sudo live validation completed on `85b7f7d`. Remaining manual checks are recorded in the [validation record](../validation/interactive-secret-entry.md); gate results and implementation decisions follow below.
Depends on: plan destination binding and Infisical write confirmation
Prepares for: agent-orchestrated credential creation through future provider integrations

## Goal

Make adding one externally created credential easy: one command, a clear destination, a hidden value prompt, and one confirmation. No manual import file, no plan ID to copy when starting from scratch, and no provider credentials on the client.

Agents may orchestrate secret operations without seeing values or approving their own operations. Separate three responsibilities:

| Responsibility | First release | Later |
| --- | --- | --- |
| Initiate | Agent prepares a saved plan, or human starts `secretops add` | Same |
| Supply the value | Human uses a hidden prompt; existing generation remains separate | A broker integration may obtain a credential directly from a provider |
| Authorize | Human confirms the exact broker plan through human access | Separately designed, limited standing authorizations |

This change implements only prompt-based entry. It does not redesign generation or implement provider key creation, standing authorizations, rotation, certificate/SSH workflows, or GitHub sync. GitHub sync remains a separate saved plan.

## User experience

Proposed human-facing commands:

```text
secretops add
secretops add SCALEWAY_API_KEY --env staging
secretops add --plan PLAN_ID
```

The first form asks for missing metadata. The second prepares the plan automatically. The third uses an agent-prepared plan and refuses conflicting name/environment arguments. All forms use the same broker workflow.

Example, with synthetic names and a configured broker alias:

```text
$ secretops add SCALEWAY_API_KEY --env staging

Broker:      nas
Vault:       infisical / configured-project / staging / /
Server:      https://vault.example.test
Secret:      SCALEWAY_API_KEY
Operation:   create only; refused if present at the final check
Plan:        PLAN_ID

Checks passed. The secret is currently absent.
Value (hidden):
Save? [y/N]: y

Saved and verified.
```

The operation text reflects the adapter's declared capability. For a non-atomic adapter, use the wording above. Explain the remaining race with other writers in `--help` and operating documentation rather than repeating a warning at every entry. Only an adapter that guarantees atomic creation may say that an existing secret is never replaced by this operation. Do not expose adapter configuration questions in the normal input flow.

Examples of safe result messages:

```text
Secret already exists. Nothing was written by this invocation.

Write outcome is unknown. Do not submit again yet.
Check broker status for plan PLAN_ID; if unresolved, ask the operator to verify it.
```

- One-time setup records a broker connection and human access method. With multiple configured brokers, require an explicit choice rather than silently choosing a destination. Reuse the SSH configuration; do not build an SSH credential manager.
- Broker configuration and the saved plan determine the actual server, project, folder, and environment. The local alias is a convenience, not authority. Display actual binding fields, not only a friendly project label.
- Run plan validation, policy, connection, and existence checks before requesting the value. Planning itself still makes no provider calls; these early checks are preflight.
- Read from the human's controlling terminal with echo disabled and restore terminal settings on every exit. No visible characters, stars, value length, preview, or repeated-entry prompt. Fail if secure input is unavailable; never fall back to echoed input or arbitrary stdin.
- Default confirmation is no. Do not offer `--yes`, `--value`, an environment-variable value source, file input, or a raw stdin mode in this release.
- Keep the final confirmation adjacent to the reviewed metadata; repeat the summary if intervening prompts or output could obscure it. Values are sent only after confirmation.
- Use short, actionable messages. A confirmed write prints the name and safe metadata if useful, never provider output. On uncertainty, give the plan ID and explain that the operator must check state before any new attempt.
- A caller does not need a separately installed `infisical` CLI or vault token. The installed client invokes the configured broker's human channel.

## Action and saved plan

Proposed action: `vault.secret.add`, requiring human approval even in development environments.

Its metadata-only recipe adds:

```yaml
action: vault.secret.add
secret_name: SCALEWAY_API_KEY
env: staging
create_mode: create_only
value_source: human_prompt
```

The saved plan includes these fields, the existing vault destination binding, plan ID, and contract version. It contains no value, value hash, length, submission token, or credential. Only `human_prompt` is accepted for this new action. Do not add `value_source` to existing generation plans merely to anticipate future integrations.

Use the existing canonical digest (`sha256-v1`) over operation metadata, including `value_source`. The digest approves the destination and operation; it does not commit to the secret value. The value supplied in the confirmed human invocation is the value authorized for this operation.

The implementation must explicitly choose and document contract-version compatibility before coding. The current parser uses a global version and strict per-action schemas: adding this action is not permission to reinterpret old approved plans. Preserve existing action semantics; if the global contract version is bumped, document that outstanding plans require replanning and approval. Do not silently migrate approvals.

The default policy explicitly denies `vault.secret.add` in production, just as it denies generation, rotation, import, and copy writes there. Human confirmation or an approved plan cannot override that denial. Existing environment-policy checks remain in force.

## Human access and agent access

The existing agent forced command remains metadata-oriented: `write-recipe`, `plan`, `show-plan`, `preflight-plan`, and execution of permitted approved operations. Plan this action through those existing commands; do not add an agent `add`, approval, or secret-submission command.

For this action, ordinary `execute-plan` without a value refuses before a write and explains that human completion is required. It must not wait for the agent to provide stdin or create an alternative way to bypass human access. The first release completes approval and execution together in the human invocation; there is no pending value for an agent to collect or resume.

For `vault.secret.add`, ordinary `approve-plan` refuses with "use secretops add --plan PLAN_ID"; approval is recorded only during authenticated submission. Human `reject-plan` remains available without entering a value. Rejection and submission participate in the same per-plan lock so a rejected plan cannot subsequently be submitted.

Remote human access reuses the operator's existing human SSH access described in BROKER_HOST.md: an unrestricted human key or the operator's own account with sudo. The client may use that existing connection to invoke a fixed installed broker endpoint; a second human key is not required. Existing human `show-plan`, `approve-plan` for other actions, and `reject-plan` remain available through that access. The special approval rule for `vault.secret.add` above still applies.

A narrow human forced-command wrapper is an optional additional restriction, not a prerequisite for the first release. If installed, it must allow the preparation/review/submission operations below and human `reject-plan`, with exact argument validation. It must not inadvertently remove the operator's documented approval/rejection access for other workflows; that access may remain through their existing administrative route. The optional wrapper does not replace or broaden the agent wrapper.

Agent access must fail all human submission/approval endpoints regardless of TTY allocation. The client uses fixed endpoint commands and validated arguments, without `eval`, interpolated shell code, client-selected broker executables, path traversal, or forwarding. Reusing unrestricted human SSH access does not make that credential restricted; its protection remains the operator's responsibility. SSH host-key verification remains enabled; no remote PTY is allocated for secret transport.

Local human access may use an operator's existing sudo authentication to invoke the installed human endpoint as the broker user. Authenticate before showing the secret prompt; do not place a sudo password prompt on the secret input stream. Avoid a broad passwordless sudo grant. Document the exact supported invocation during implementation and installation testing.

TTY checks and confirmation prompts are UX, not identity verification. If an agent and human share an OS account, the agent may access the human's SSH agent, keys, terminal, or cached sudo authority. This first release accepts and documents that limitation; it does not claim isolation from a hostile process under the same account. FIDO user-presence/verification or per-use SSH-agent confirmation can strengthen access but are optional, not proof that a human reviewed this specific operation.

## Broker exchange

Keep this a small, versioned request/response protocol, not a daemon or general RPC framework. Proposed human broker endpoint operations are `prepare-add`, `review-add`, and `submit-add`; these are internal endpoints, not extra daily-use commands.

1. `prepare-add` accepts strictly validated name/environment metadata and creates a saved plan through normal planning. Alternatively, `review-add` loads an existing plan by ID. Broker returns a strict, versioned metadata response containing the reviewed plan and its full current digest. No caller-supplied destination overrides the broker binding.
2. Review performs early preflight and returns safe results. The client renders the broker's validated copy, collects input privately, and obtains confirmation for that exact digest.
3. `submit-add` receives one bounded request over SSH stdin or a local pipe. It carries protocol version, plan ID, the full reviewed digest, and the value. None of the value travels in command arguments, SSH command strings, environment variables, or files on the client. Disable diagnostic dumping of request bodies.
4. Broker validates framing and input, loads one validated plan, checks the reviewed digest, current destination, action/source/mode, policy, and executable status, and performs preflight again. Approval is recorded only through this authenticated human path. Execution uses the same checked plan object and recorded destination, with the existing approval-digest protections.
5. Broker executes the write once, performs exact internal read-back comparison, records a safe result, and returns only metadata. Use the creation contract below, preserving existing write-confirmation semantics; a value hash is unnecessary for comparison and must not be returned or persisted.

Proposed wire format: one UTF-8 JSON object followed by EOF, with a 64 KiB total request cap. Reject duplicate/unknown fields, wrong types, unsupported versions, invalid UTF-8, trailing data, and oversized or incomplete requests before any write. Responses have a strict safe schema; client errors never echo raw responses. Render metadata safely so control characters cannot alter the terminal display.

Value constraints: at most 16 KiB in UTF-8; one nonblank line; reject CR, LF, NUL, and unsupported control characters. Preserve meaningful spaces and Unicode exactly; no trimming or normalization. Input submission removes only the terminal's Enter delimiter, not value content. Empty and whitespace-only values are rejected before provider launch.

Multiline paste must be rejected as a whole, never stored as its first line. Implement and test bracketed-paste-aware terminal input: consume a marked paste through its end marker and reject the entire value if it contains a newline. A partial paste, including a missing end marker, cannot advance to confirmation. Paste data, including a pasted `y` on another line, must never be interpreted as confirmation. Before displaying `Save?`, discard pending value-input state and require a fresh confirmation; confirmation input also rejects paste events. Restore terminal modes, including bracketed-paste mode, on exit.

A plain single-line `getpass()`, `tcflush` alone, or checking for extra bytes within a fixed interval (such as 100 ms) cannot establish that all pasted input has arrived. Delayed input can cross that interval. Do not use timing as the safety boundary.

The terminal-input implementation must demonstrate a supported bracketed-paste path. Before implementation, choose and document whether terminals without that support are refused or have an explicitly restricted manual-entry mode. Do not claim that unmarked typing and paste can reliably be distinguished. If safe behavior cannot be demonstrated, stop and revise the input design rather than silently weaken multiline protection.

Bound incomplete requests with a receive deadline (proposed 120 seconds). This starts when submission begins, not while the human is reviewing or typing. A valid value is held only for the immediate operation. Do not add a staging file, persisted value reference, or resumable value store. The existing adapter may require a broker-private temporary YAML file; retain its restrictive permissions and cleanup, including failure paths. Memory copies cannot be reliably erased in Python; do not promise otherwise.

## Vault creation contract and future backends

Add one narrow adapter operation, proposed as `create_secret(name, env, value)`, returning a creation result:

| Result | Adapter guarantee |
| --- | --- |
| `CONFIRMED` | The attempted creation was followed by exact read-back matching the submitted value |
| `EXISTS` | A trustworthy presence check or atomic conflict caused refusal; this invocation did not write |
| `UNKNOWN` | A write may have occurred and exact confirmation is unavailable |

Failures before write launch raise the existing safe `VaultError` hierarchy. Keep this creation result separate from the existing `WriteOutcome` used for upserts; do not imply that existing generation, rotation, copy, or import become atomic as a side effect of adding this operation.

The adapter declares `atomic_create` as a capability of the configured implementation, not an option a client can assert. The new action uses this operation for its final create decision instead of implementing backend-specific check/upsert logic in the client or lifecycle layer. Early preflight still uses an existence check for UX. Record any capability change that alters approved-operation semantics under the contract-version rules.

Initially implement only Infisical: if no atomic provider path is accepted, its creation operation checks then writes and declares `atomic_create = false`. A later OpenBao adapter can implement conditional creation (intended KV v2 CAS-zero mapping), subject to provider-version and data-layout validation, without changing the prompt, human channel, or result model. No OpenBao integration is included in this PR.

`vault_destination` already contains `backend` and participates in the digest. Retain that design; future backend-specific destination schemas and path mapping are a separate change. A plan for Infisical must never be retargeted to OpenBao. OpenBao planning will need an explicit per-secret record layout for conditional creation; do not assume that multiple logical secrets sharing a record have independent create semantics.

Tests assert this common contract and the actual capability each adapter declares. Include simulated atomic and non-atomic implementations without adding a second real provider integration. In particular, document the race in the non-atomic case rather than require a guarantee it cannot satisfy.

## Concurrency and create-only

Early existence checks improve UX; they do not prove absence at write time. The current Infisical adapter checks existence and then invokes an upsert. This is not atomic create-only.

Before implementation, investigate whether the pinned provider/CLI can perform a narrowly scoped atomic create with conflict detection. Do not introduce a new API integration implicitly. Record the tested version and capability. If atomic create is unavailable, either retain the existing documented check-then-write limitation with explicit acceptance, or stop for a scope decision. Never advertise an overwrite guarantee the adapter cannot enforce.

The human completion path also needs a per-plan exclusive lock spanning final validation, approval, write, and result persistence. Concurrent human submissions and ordinary lifecycle mutations of the same new-action plan must respect it or refuse. A second submission must not reuse an executed, rejected, or failed plan. This is a narrow requirement for the new action, not a claim that all existing workflows become concurrency-safe. A plan lock does not stop another plan, process, or provider user from racing the same secret name.

## Outcomes and interruption

| Outcome | Meaning and user guidance |
| --- | --- |
| `created` | Exact read-back matches the submitted value; plan becomes `executed` |
| `exists` | Final check or a trustworthy atomic conflict reports presence; no write by this invocation; never suggest automatic replacement |
| `cancelled` | Human declines or cancels before sending the value; no write request submitted |
| `refused` | Invalid plan/digest/destination, policy denial, or invalid input; no write started |
| `provider_error` | Credentials, availability, or preparation failed before write launch; no write started |
| `unknown` | A write may have occurred but confirmation is unavailable; do not retry automatically |

Use existing plan statuses rather than adding an outcome for every user message. The outcome `refused` is distinct from plan status `rejected`, which means a human explicitly rejected the plan. Invalid input detected before execution leaves a valid plan `planned` and unapproved, allowing corrected input in a fresh reviewed attempt. Early review failures do not approve the plan. Before submission, cancellation leaves the plan `planned` and unapproved, whether the human or an agent originally prepared it. The plan can be reused for another reviewed attempt. Do not introduce automatic rejection or cleanup based on plan origin; stale-plan cleanup is deferred. Invalid plan files remain untouched. Final execution failures follow existing validated-plan failure behavior; `unknown` is a failed execution with an explicit audit outcome, never a confirmed absence.

Read-back mismatch does not identify a cause: provider transformation, an adapter defect, or a competing writer may be responsible. Even an atomic create can be followed by another writer before read-back. Therefore `UNKNOWN` remains necessary for atomic adapters; neither an atomic conflict nor read-back mismatch justifies an automatic retry.

Ctrl+C before submission is clean cancellation. Once any submission may have reached the broker, loss of connection or interruption is conservatively unknown to the client, even if the broker eventually records a more precise result. Do not promise rollback on disconnect. Preserve a safe local message with plan ID and instructions to inspect broker status. An executed plan may resolve uncertainty about a lost response; an unresolved write needs human investigation before a fresh plan. Never automatically resubmit or resume with a buffered value.

## Audit and sensitive data

Record only allowlisted metadata: human channel, plan ID, action, names, environment, destination metadata already allowed in audit, reviewed/approval digest, and outcome. Channel must come from the trusted endpoint, not a request field. Reuse existing audit conventions and document any schema extension.

Never record value content, length, hash, request body, terminal input, provider output, or credentials. An approval digest covers metadata only. Do not infer a verified human identity merely from a `human` channel label; the configured access boundary provides that assurance to its documented extent.

## Implementation and documentation scope

- Add the action, strict recipe/plan schema, policy rules, and preflight/execute refusal without supplied input. Reuse destination resolution, digest canonicalization, name validation, and vault outcomes. Add the narrow creation contract and declared atomic-create capability without a backend framework or changes to unrelated actions.
- Add a small client command, bounded protocol, human broker endpoint, per-plan completion lock, and tests. Keep prompting/transport separate from domain behavior so another language can implement the same contract later.
- One-time human-channel configuration must be documented and reproducible, reusing existing human SSH/sudo access without requiring another key. Client setup contains connection metadata, not vault tokens. Installation includes the matching application and required wrappers; document any optional human wrapper separately.
- Update README, AGENT_USAGE, BROKER_HOST, SECURITY_MODEL, ARCHITECTURE, and TODO with actual commands, shared-account limits, create-only capability, recovery guidance, and production restrictions. Mark proposed commands unavailable until implemented.
- Preserve existing generate/import/copy workflows. No automatic GitHub sync, automatic provider-key creation, generic wizard framework, or standing authorization in this PR.

## Acceptance criteria

Automated, using synthetic values and fake providers:

1. Both direct and agent-prepared flows produce metadata-only, destination-bound plans. Planning needs no token or provider call. Existing generation semantics remain unchanged. The default policy denies `vault.secret.add` in production, including with human confirmation or approval, with zero provider calls on denial.
2. The human sees the broker's current validated metadata. Changed content or destination between review and submission is refused before provider calls; approval covers the exact full reviewed digest.
3. Agent SSH access denies all human endpoints with or without a PTY. Ordinary `execute-plan` cannot supply or request an input value. Ordinary `approve-plan` redirects to human completion, while human `reject-plan` can reject without input and blocks subsequent submission. Shared-account limitations are documented rather than claimed solved by these tests.
4. Early existence/policy/authentication failures occur before the prompt. Final checks are repeated. Existing names are refused; contract tests cover `CONFIRMED`/`EXISTS`/`UNKNOWN`, safe pre-launch errors, and atomic/non-atomic race behavior. Assert only the actual documented adapter guarantee; even an atomic create can have an unknown read-back result.
5. Empty, whitespace-only, multiline paste, unsupported controls, oversized input, malformed framing, incomplete requests, and receive timeout fail without a write. Unicode and meaningful spaces round-trip unchanged. PTY tests include delayed/chunked paste, a second pasted line containing `y`, incomplete paste markers, paste at confirmation, and the selected behavior without bracketed-paste support.
6. Decline, EOF, and Ctrl+C before submission cause no write and leave no submitted value. Plans remain `planned` regardless of who prepared them. Terminal echo and paste modes are restored after success, validation failure, and interruption.
7. A matching read-back confirms; a post-launch timeout, failure, or mismatching read-back yields unknown. A disconnect after submission never triggers automatic retry or a false cancellation claim.
8. No synthetic value appears in arguments, environment, client files, plans, output, audit, errors, or protocol diagnostics. Broker-only adapter files have restrictive permissions and are cleaned up. Test fixtures capture transport without printing values.
9. Concurrent submissions for the same plan cannot both write; terminal plans cannot be replayed. Invalid input before execution leaves a valid plan `planned`; only explicit human rejection produces status `rejected`. Incompatible protocol/contract versions fail safely. Fixtures pin metadata, digest, wire framing, and outcomes independently of Python internals.
10. Installation instructions work in a clean environment using existing human SSH/sudo access; no second key is required. Missing human access produces a useful setup instruction before prompting for a value. If the optional human wrapper is tested, verify that rejection remains available and agent permissions remain unchanged.

Manual, after review and implementation:

11. Install the application and matching wrappers on the existing broker while preserving credentials, policy, plans, audit, and SSH settings. Record client, broker, and Infisical CLI versions.
12. Using synthetic names in dev/staging, test direct entry, agent-prepared entry, cancellation, existing-name refusal, and multiline-paste rejection in a supported terminal. The human supplies values and approves; agents observe only safe metadata.
13. Change the configured destination between review and submission and confirm refusal; restore configuration. Verify the value privately through the broker's read-back result, without provider listing or value output.
14. Confirm the agent channel cannot submit/approve. Record only redacted evidence in the repo, actual coverage and remaining limitations; leave GitHub sync to its separate plan and validation.

## Review gates before implementation

Resolve and record these decisions in this document before starting code:

- Actual pinned Infisical atomic-create capability and whether its non-atomic fallback is accepted.
- Exact endpoint invocation over existing human SSH/local sudo access, and terminal input approach, including demonstrated multiline-paste handling and the decision to refuse unsupported terminals or provide a restricted manual-entry mode. A narrow human wrapper remains optional; no additional human key is required.
- Contract version compatibility, lock participation, and exact outcome-to-status/exit-code mappings.

### Gate closure work plan

The design through commit `c6e0a5c` has received reviewer approval, subject to closing the three gates above. Approval of the design does not mean these gates have been demonstrated. All three remain open. Close gate 1 before finalizing gate 3, because the provider guarantee affects conflict handling and operation wording. Gate 2 is independent and may proceed in parallel.

1. **Provider capability investigation (open).** Inspect the source and documented behavior of the pinned Infisical CLI version, including the machine-token write path. Establish whether the available create operation rejects an existing secret atomically or performs an upsert. Record the version actually installed on the broker, how it was established, and the source revision inspected; an earlier validation record is historical evidence, not proof of the current installation. Obtain version metadata through an approved operator check without secret access; do not bypass the project provider-CLI restrictions. Record source references, the supported command/path, actual guarantee, and the explicit decision on accepting a non-atomic fallback. If installation does not currently pin that CLI version, include a pinning and upgrade/revalidation decision: a trusted executable path alone does not pin behavior across upgrades. This investigation requires no credentials, provider secret access, or writes. A possible new API integration requires a separate scope decision rather than silently expanding this PR.
2. **Terminal and human-channel prototype (open).** Build a small isolated PTY prototype using synthetic input only, with no vault or broker credentials. Keep the prototype, its focused PTY tests, and reproduction instructions under `experiments/interactive-secret-entry/`, outside `src/secretops` and the installable package. Verify exclusion from built distributions before shipping. Do not connect it to `add` or the broker protocol yet. Retain useful input logic and scenarios for deliberate promotion into the production module and test suite after review; remove the superseded prototype at that point rather than maintain two implementations. Demonstrate single-line entry, rejection of multiline and delayed/chunked paste, a pasted second line containing `y`, incomplete paste markers, paste at confirmation, Ctrl+C/EOF, and restoration of echo and paste modes. Record a terminal matrix in the validation record, with application/version, transport or multiplexer, tested scenarios, and result. Start with the actual T14 client terminal (the session history shows GNOME Terminal; confirm the application and version before claiming support). If used, add tmux with its version and relevant configuration, and an SSH session into the client computer where the input application runs; otherwise mark them untested and unsupported for this release rather than assume compatibility. The SSH transport from the client to the broker remains a separate, non-PTY channel. Exercise each supported combination, because multiplexers can change bracketed-paste forwarding. Run the automated PTY harness too; simulated markers alone do not prove terminal support. Choose and document behavior for unsupported terminals without claiming that unmarked paste is distinguishable from typing. Specify exact endpoint invocations over existing human SSH and local sudo access, including authentication before secret entry, host-key verification, fixed commands, and transport without a remote PTY. The prototype does not install endpoints, change SSH/sudo configuration, or contact the live provider.
3. **Execution contract decisions (open).** Record the contract-version compatibility decision and a complete table mapping each outcome to plan status, audit outcome, and client exit code. Include early refusal, invalid input, explicit human rejection, missing input on ordinary execution, provider failure before launch, post-launch uncertainty, cancellation, and a lost response. Define which commands acquire the per-plan lock, its lifetime, behavior on contention and process death, and how rejection and submission exclude each other. Preserve the distinction between client uncertainty and a subsequently recorded broker result. Keep these contracts independent of Python implementation details.

Append evidence and decisions to this document, or link to a repository validation record and reproducible prototype, then obtain a short final review before implementing the workflow. Do not mark a gate complete based only on a proposed approach. If a demonstration changes the design, update the relevant sections and acceptance criteria together.

The estimated 4–6 working days for the complete change includes this preparatory work, implementation, tests, documentation, review, and broker validation; it is not an additional 4–6 days after closing the gates. Reassess the estimate if atomic creation requires a new provider integration or terminal requirements change materially.

No implementation, broker changes, provider calls, prototype execution, or live validation have been performed as part of this draft.

## Gate results

Recorded with the implementation. The owner asked for the change to be completed in one pass, so the gates were closed by source inspection, automated PTY and endpoint tests, and the decisions below, and the final review happens on the pull request together with the code. Items that need a real broker or a real terminal are listed under "Still to verify by hand"; they are not claimed here.

### Gate 1: Infisical create capability (not atomic; installed version verified)

- Source inspected: Infisical CLI tag `v0.43.99` (repository `Infisical/cli`), `packages/cmd/secrets.go` and `packages/util/secrets.go`. v0.43.99 is the version recorded by the 2026-10-17 NAS validation; that record is historical evidence of what was installed then, not proof of the current installation.
- Machine-token path: `secrets set` with a service or universal-auth token calls `util.SetRawSecrets` with the raw arguments. `SetRawSecrets` fetches the folder's secrets (`GetAllEnvironmentVariables`), then for each name either queues an update (present, value or tags changed) or a create (absent), and runs the creates (`CallCreateRawSecretsV3`, `POST /v3/secrets/raw/{name}`) and updates (`CallUpdateRawSecretsV3`) one by one. There is no flag that refuses an existing secret.
- Guarantee: an upsert. A name created by another writer between our final check and the CLI's own listing is overwritten. A name created between the CLI's listing and its create call reaches the server's create endpoint, whose conflict behaviour was not verified here; the adapter then sees a non-zero exit or a read-back mismatch and reports `UNKNOWN`.
- Decision: accept the non-atomic fallback. `InfisicalCliVault.atomic_create = False`; `Vault.create_secret` checks and then writes. The client shows "create only; refused if present at the final check". A direct API create would need a new API integration and is out of scope.
- Pinning: installation does not pin the CLI version; `SECRETOPS_INFISICAL_BIN` fixes the path, not the behaviour. Decision: the broker host documentation names v0.43.99 as the inspected version, and an upgrade of the CLI requires repeating this source check and the read-back validation before relying on the documented behaviour. The installed version is recorded during the live validation (operator check).

### Gate 2: terminal input and human channel (automated evidence; manual matrix open)

- The prototype step was folded into the production module: `src/secretops/terminal_input.py` with headless PTY tests in `tests/test_terminal_input.py`. No `experiments/` copy was created, so nothing needs removal and nothing extra ships.
- Input: `/dev/tty` only, echo, canonical mode, and signals off (Ctrl+C, Ctrl+D, Ctrl+Z are handled as cancellation, also inside a paste or an escape sequence), bracketed paste on. A marked paste is consumed through its end marker; a line break inside it rejects the whole value. A paste without an end marker never reaches confirmation; Ctrl+C ends the wait. Before `Save?`, unread input is discarded; at `Save?`, a paste counts as no, and only the letters of yes/no are echoed (any other character means no and is not shown). Terminal settings and paste mode are restored on every exit.
- Unsupported terminals (revised after review of `226d8d8`): the first version accepted them as a "restricted manual-entry mode". Review showed with a PTY that an unmarked multiline paste then ends the value at its first line and its next line is echoed at, and can answer, `Save?`. Decision: refuse them. After enabling bracketed paste, the client sends a DECRQM query for mode 2004 and requests the value only when the terminal reports the mode set (1) or permanently set (3). No answer within 1.5 seconds, or any other report, refuses entry before any prompt; input received before that point is discarded. The wait decides only support, never safety: a late or missing answer refuses. Remaining trust: a terminal or multiplexer that reports the mode but does not mark pastes would defeat the check.
- PTY tests cover: hidden typed entry, single-line paste, multiline paste, a pasted second line `y`, delayed and chunked paste (including markers split across writes), a missing end marker and Ctrl+C during it, Ctrl+C inside an escape sequence, paste at confirmation, non-answer characters at confirmation, typed-ahead input before confirmation and before the prompt, Ctrl+C/Ctrl+D, editing keys, Unicode and spaces, a closed terminal, restoration of settings, and refusal when the terminal does not answer, reports the mode off, or receives an unmarked multiline paste. Simulated reports and markers do not prove that a given terminal emulator sends them.
- Human channel: the client runs fixed endpoint commands through existing access. SSH: `ssh -T -o RequestTTY=no -o ForwardAgent=no -o ForwardX11=no -o ClearAllForwardings=yes <destination> /usr/local/bin/secretops-broker <endpoint> <validated arguments>`; host-key checking follows the user's SSH configuration and is never disabled. SSH to a personal account then sudo (added after review): `... <destination> sudo -n -u <broker user> -- /usr/local/bin/secretops-broker <endpoint> ...`, which needs a narrow sudoers rule for the three endpoints because sudo cannot prompt without a PTY. Local: `sudo -u <broker user> -- /usr/local/bin/secretops-broker <endpoint> ...`; the first call (review) happens before the value prompt, so sudo authenticates first, and sudo reads its password from the terminal, never from the request stream. A `command:` entry runs a local command as the same user and is for development only. No optional narrow human wrapper is shipped.

### Gate 3: execution contract (closed)

Contract version: stays 1. The new action and the optional `value_source` field are additions; the meaning of every existing plan is unchanged, and `value_source` is accepted only for `vault.secret.add`.

| Situation | Plan status | Audit (`status` / `outcome`) | Client exit |
| --- | --- | --- | --- |
| Review ready | `planned` | `success` / `ready` | continues |
| Policy denial at prepare (for example production) | no plan saved | none | 1 |
| Early refusal at review (binding, policy, status) | unchanged | `failure` / `refused` | 1 |
| Secret exists at review | `planned` | `failure` / `exists` | 1 |
| Provider error at review | `planned` | `failure` / `provider_error` | 1 |
| Invalid value or request | unchanged (`planned`) | none | 1 |
| Cancelled or declined before submission | `planned` | none | 2 |
| Plan changed, destination changed, or policy denial at submission | unchanged | `failure` / `refused` | 1 |
| Human `reject-plan` | `rejected` | none (existing behaviour) | n/a |
| Ordinary `execute-plan` | unchanged | `failure`, `failure: human_completion_required` | n/a |
| Ordinary `approve-plan` | unchanged | none | n/a |
| Approval at submission | `approved` with the reviewed digest | `success` / `approved` | continues |
| Provider error after approval, before write launch | `failed` | `failure` / `provider_error` | 1 |
| Exists at the final check | `failed` | `failure` / `exists` | 1 |
| Write confirmed by read-back | `executed` | `success` / `created` | 0 |
| Write not confirmed (exit status, read-back mismatch, unexpected error) | `failed` | `outcome_unknown` / `unknown` | 3 |
| Lost or unreadable submission response | as recorded by the broker | as recorded by the broker | 3 |

All `vault.secret.add` audit events from the human endpoints carry `channel: human`, set by the endpoint. The ready review and every submission event after a parsed request carry `reviewed_digest`; the approval event also carries `approval_digest` (the same value, now stored in the plan). A lost response is unknown to the client even when the broker recorded a precise result; `show-plan` then shows the recorded status.

Lock: `submit-add` and `reject-plan` (for this action) take an exclusive, non-blocking `flock` on `.<plan_id>.lock` in the plan directory, for the whole check-approve-write-record sequence. Contention is refused ("plan is in use"), never waited for. The lock ends with the process, including when it dies. The plan is read again under the lock, so a rejected or completed plan cannot then be submitted. Review and prepare do not lock; they do not change status.

### Still to verify by hand

- Remaining manual acceptance checks: full broker cancellation/decline, changed destination, live denial of human endpoints through agent access, and independent audit validation. These were not established by successful entry alone.
- Complete terminal identification: the actual T14 session passed support-query, single-line paste, multiline rejection, Ctrl+C, and decline checks, reporting `VTE_VERSION=6800`. The application name/version is not yet confirmed. tmux and SSH-into-client have not been tested and carry no support claim.
- Human submission over direct SSH and SSH to a personal account followed by sudo. Local sudo submission has passed; agent SSH metadata operations do not establish human SSH submission compatibility.

Live evidence recorded on 2026-10-18 confirms installed Infisical CLI `0.43.99`, direct and agent-prepared creation in `dev` with read-back confirmation, and existing-name refusal through local sudo. Gate 1's installed-version check is complete. Gate 2 has evidence for the tested session and local-sudo path; the remaining matrix/access checks are open. See [results and evidence limits](../validation/interactive-secret-entry.md). This does not mark all manual acceptance criteria complete.
