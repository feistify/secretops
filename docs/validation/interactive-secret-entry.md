# Interactive secret entry validation

Date: 2026-10-18
Tested client and broker application: commit `85b7f7d`
Broker wheel SHA-256: `4aee06d6e10fb517f6655413cb7b9f6daecd43848ab1bd808fa628f90dd01df9`
Installed Infisical CLI: `0.43.99`, confirmed by a human version-only command as the broker user during this validation.

Manual validation scope: human secret entry through **local sudo only**. Human entry over SSH (direct or followed by remote sudo) and use inside **tmux have not been tested**. Existing agent SSH metadata checks do not establish either of those capabilities.

## Setup

A human upgraded the dedicated local broker application from the built wheel. The installer retained the previous application and policy for recovery. Existing wrappers already matched the source and were not replaced. Credentials, login state, plan store, audit, runtime, provider binaries, and SSH configuration were preserved.

The existing broker policy did not include the new action. The human explicitly enabled `vault.secret.add` for `dev` only, requiring approval and denying production. All existing policy rules were preserved; this run did not enable the new action in `staging`.

The client used a temporary metadata-only configuration selecting local sudo to the broker user. Values were supplied by the human in their own terminal. The agent used the existing restricted SSH channel only to upload a metadata-only recipe, plan, preflight, and inspect final plan metadata. No agent approval or value submission was used.

## Terminal matrix

| Client session | Reported environment | Result |
| --- | --- | --- |
| Actual local T14 terminal, emulator name/version not independently confirmed | `TERM=xterm-256color`, `VTE_VERSION=6800`, no `TERM_PROGRAM`, no tmux or SSH TTY indicated | Passed the four manual checks below |
| tmux | Not exercised | No support claim |
| SSH session into the client computer | Not exercised | No support claim |

The environment reports VTE `0.68.0`; it does not establish the terminal application's name or version. The result applies to the actual tested session, not all VTE terminals or versions.

A human ran a standalone check using the production `HiddenTerminal` implementation, with synthetic input and no broker/provider access:

| Check | Result |
| --- | --- |
| Terminal confirms mode 2004 before prompting | Passed; entry proceeded after the support query |
| Single-line paste followed by typed confirmation | Accepted; exact synthetic text matched and confirmation was true |
| Two lines pasted together, with `y` as the second line | Entire paste rejected; it did not reach confirmation |
| Ctrl+C at the value prompt | Cancelled |
| Typed value followed by `n` | Confirmation declined |

The human supplied the PASS summary. The check printed only synthetic fixture text, safe results, and terminal metadata; entered values were not echoed. These manual checks do not independently inspect every terminal setting after exit.

## Real broker results

| Check | Result |
| --- | --- |
| Interactive `secretops add` without name/environment arguments | Human supplied `NASCHECK_INTERACTIVE_85B7F7D` and `dev`, reviewed the destination, entered a synthetic value, and confirmed. Client reported `Saved and verified.` |
| Repeat the same name | Client reported `Secret already exists. Nothing was written.` before requesting any value |
| Agent-prepared plan | Agent uploaded a metadata-only `vault.secret.add` recipe for `NASCHECK_INTERACTIVE_AGENT_85B7F7D` in `dev`, then saved and preflighted a new plan through restricted SSH. Preflight confirmed absence |
| Human completes that plan | Human ran `secretops add --plan ...` through local sudo, reviewed the destination, entered a synthetic value, and confirmed. Client reported `Saved and verified.` |
| Inspect agent-prepared plan after completion | Restricted `show-plan` reported `executed`, `secret_value_included: False`, `value_source: human_prompt`, and equal current/approval digests |

`Saved and verified` means the broker's adapter reported a matching internal read-back, not an independent human inspection of provider values. No value was returned to the agent. The repeat-name result demonstrates refusal when the name is already present at the check; it does not demonstrate atomic creation against a competing writer.

## Automated evidence and remaining limits

- Review of `85b7f7d`: 114 relevant tests passed across terminal input, client, broker entry, and vault creation. Final pre-PR verification on `interactive-secret-entry`, with application code unchanged since `85b7f7d`: all 494 pytest tests and all 41 wrapper checks passed.
- The upgrade bundle passed checksum and isolated import/command-registration checks. Installation and recovery after an injected post-swap failure were exercised against temporary application/policy fixtures before the human upgraded the real broker.
- Infisical CLI `0.43.99` matches the source-inspected version. Its create path remains non-atomic check-then-upsert; source review and this live run do not establish an overwrite guarantee against concurrent writers. CLI upgrades still require revalidation.
- Real secret submission was tested through local sudo only. Human submission over direct SSH and SSH followed by remote sudo remain unverified. Restricted agent SSH was exercised for metadata operations, not human submission.
- Changed-destination refusal for this action, provider failures, unknown outcomes, replay/concurrency, rejection through `reject-plan`, and denial of human endpoints through the agent wrapper were not repeated on the live broker. Automated evidence remains distinct from live evidence.
- Cancellation, declined confirmation, and multiline rejection were exercised with the standalone production-terminal check, not as complete live broker operations. The new action was not tested in `staging` or production.
- This session did not independently inspect the broker audit file, trace subprocess environments, or instrument provider network calls. It therefore does not establish live audit-content validation or independently measured zero provider calls on refusal.
- No GitHub sync was part of this run. The complete human-entry-to-GitHub workflow remains a separate validation task.
- Manual checks used temporary helpers/configuration. A clean installation following repository-only instructions remains open. Synthetic test secrets remain available for human cleanup.

Gate 1's installed-version check and the tested local-sudo portion of gate 2 now have live evidence. Emulator identification, other terminal/access combinations, and the remaining manual acceptance checks are still open; this is a partial validation record, not blanket completion of criteria 11–14.

No secret values, value hashes, credentials, private server addresses, or project identifiers are included in this record.
