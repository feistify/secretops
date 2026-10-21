# Design: GitHub target reliability

Status: implemented and merged into `main` in PR #9; the complete local-sudo workflow passed on `d0d17aa`. See [live results, version provenance, and evidence limits](../validation/github-target-reliability.md).
Depends on: isolated GitHub CLI environment, destination-bound plans, and interactive secret entry

## Goal and scope

Complete the daily-use workflow: a human enters an externally created credential, the broker stores it in the vault, and an agent transfers it to GitHub through a separately approved plan. Calls finish within a configured timeout, failures do not imply that a target was unchanged, and partial sync results remain useful without exposing values.

This PR covers `github.env_secret.write`, `github.env_secret.sync_many`, and metadata-only `github.env_secret.verify_many`. It also aligns unknown-outcome exit codes for existing vault execution paths; their write semantics and adapters are otherwise unchanged. Keep existing authentication, binary selection, environment isolation, explicit destinations, policy, human approval, and stdin-only value delivery. No new target, authentication path, background retry, HTTP integration, automatic sync after vault rotation, or secret-value read-back from GitHub.

## Current behavior

- `targets/github_cli.py` calls `gh` without a timeout. Writes return a boolean; metadata calls return names or `None`. Launch exceptions can escape to the CLI.
- A nonzero write exit is reported as "not updated", although the request may already have reached GitHub.
- `sync_many` reads and writes one mapping at a time, but stops with a generic failure and does not retain a safe breakdown of earlier successes, the current mapping, and later work.
- GitHub preflight currently checks source existence or target metadata shape; it does not contact GitHub. Preserve that behavior. A passing preflight does not establish GitHub authentication or availability.

## Adapter contract

Introduce a small GitHub-specific error/result contract, without redesigning the generic target base class:

| Operation/result | Meaning |
| --- | --- |
| `set_secret` returns `ACKNOWLEDGED` | `gh secret set` completed with exit status 0; use the existing CLI success criterion |
| `set_secret` returns `UNKNOWN` | A write may have occurred; no trustworthy success acknowledgement is available |
| Safe target error before write launch | No target write was started by this invocation |
| `list_secret_names` returns a list | Metadata retrieval and parsing succeeded; an empty list is a valid answer |
| Safe target error during metadata retrieval | The check failed; do not interpret it as an empty environment or missing names |

Proposed names: `GitHubWriteOutcome`, `GitHubError`, and subclasses for configuration, launch, provider, timeout, and response failures as needed. Preserve `GitHubConfigurationError` compatibility with callers. Exceptions expose only fixed, curated messages; no provider stdout/stderr, request input, credentials, or raw exception representations.

For writes, timeout, nonzero exit, termination by signal, or an unexpected failure after launch means `UNKNOWN`. Do not infer a definite rejection from a nonzero CLI exit or parse human-readable error text to guess whether the request reached GitHub. A source missing/read error, invalid local configuration, or proven executable-launch failure means no write for the current mapping. If the implementation cannot prove an exception happened before launch, classify conservatively as unknown.

For metadata calls, launch failure, timeout, nonzero exit, and malformed output are ordinary check errors, not unknown writes. Validate the structure of each row separately from the project secret-name policy. A structurally malformed row or a name invalid under GitHub naming rules is a response error. A valid GitHub name outside our stricter project pattern (for example `_FOO`) is ignored, not treated as malformed. Compare only expected names from the saved plan, respecting GitHub case-insensitive naming, and display only those expected names; never expose unrelated names. The broader provider naming rules must not relax recipe/plan validation. See [GitHub secret naming rules](https://docs.github.com/en/actions/reference/security/secrets). Preserve compatibility with the installed `gh` version and its current metadata-output format; any flag/format change must be checked against that version. Never print a raw listing or malformed line. An error must not turn into a successful check with an empty result.

`ACKNOWLEDGED` is deliberately distinct from the vault's exact read-back confirmation. GitHub metadata presence proves a name exists, not that its value matches the intended source. A metadata check must never upgrade `UNKNOWN` to `ACKNOWLEDGED`.

## Timeouts and process handling

Use the existing broker setting `SECRETOPS_PROVIDER_TIMEOUT_SECONDS`, with the same positive-integer validation and existing default (60 seconds). Resolve and validate it when constructing the target, before vault values are read. Pass the bound value to every write and metadata subprocess call. Keep the setting out of the child environment allowlist and out of the plan's destination identity.

Replace the `runner=subprocess.run` injection with `popen_factory=subprocess.Popen`. The factory accepts the executable/arguments, piped stdin/stdout/stderr, text mode, and the existing allowlisted environment; it returns a process exposing `communicate`, return code, and cleanup operations. Start the process in one explicit step, then call `communicate(input=..., timeout=...)` in another. A proven launch failure from the construction step raises a safe launch error. Once a process is returned, communication exceptions are unknown writes; do not classify every `OSError` from a combined run/communicate path as pre-launch. Metadata failures remain check errors in either phase.

Migrate all injected-runner tests to the factory/process interface, including `test_github_environment.py` and existing target tests. Capture and assert arguments and the exact environment at factory invocation, and input/timeout at communication. Preserve all current executable-resolution, token-exclusion, login-location, host/repo, and shared environment-allowlist checks. Add separate launch-failure and post-launch communication-failure cases so the test double cannot erase the boundary.

The timeout bounds each subprocess, not the entire multi-secret plan. A batch can take approximately one configured timeout per attempted call plus vault reads and local work. On timeout, terminate and reap the directly launched CLI explicitly in the `Popen` implementation; moving away from `subprocess.run` means timeout cleanup is no longer automatic. Bound cleanup/draining as well so an inherited pipe cannot turn the timeout path into an indefinite wait. Do not leave the CLI running and then retry. Do not claim remote rollback or that terminating the local CLI cancels a request already accepted by GitHub. Capture and suppress partial output, including data attached to timeout exceptions.

Configuration errors include a missing/unusable trusted binary or invalid timeout; use existing executable and environment protections. No fallback to the caller's PATH, alternative login, or raw environment propagation.

## Execution, output, and audit

Single writes and batches use the same classification and safe reporting. Execution still validates contract version, approval digest, destination, and policy before provider calls. Build the GitHub adapter before reading a value.

Process mappings in saved order. Keep one source value only for the current transfer; discard the reference afterward. A source that disappeared or cannot be read stops the batch before launching that mapping's GitHub write. Do not roll back earlier acknowledged writes.

Every mapping belongs to exactly one result category:

| Category | Meaning |
| --- | --- |
| `acknowledged` | Target CLI returned success for this mapping |
| `unknown` | Its target write may have happened; at most the current mapping in this sequential workflow |
| `not_written` | Current mapping could not be sent, for example missing source or proven launch failure |
| `not_attempted` | Later mappings were skipped after the first problem |

Identify entries by mapping index and source/target names so repeated names cannot make a result ambiguous. Counts and entries cover the complete ordered mapping list. In a single write, apply the same categories to its sole mapping.

Persist an allowlisted metadata audit event for each completed attempt before proceeding to the next mapping, followed by a final summary when possible. Events contain plan ID, action, safe destination metadata, mapping index/names, outcome, and curated failure category. The final summary contains only the four categorized mapping lists/counts and ordinary metadata. Preserve acknowledged progress if a later vault or target call fails. If audit persistence fails, stop further writes, print the known in-memory partial mapping summary, and explicitly report that the audit could not be completed. An acknowledged write stays acknowledged even if recording it fails; an audit error alone does not make that write unknown. Exit 3 if any write outcome is unknown, otherwise exit 1 for the audit failure. Attempt a safe failed-plan status update where possible, but do not claim that status or audit was persisted if storage also fails. No traceback may expose the current value or subprocess data. Process death can prevent a final event, so do not promise complete recovery evidence after arbitrary interruption.

| Final result | Plan status | Audit status | CLI exit |
| --- | --- | --- | --- |
| All writes acknowledged | `executed` | `success` | 0 |
| Failure before the current write, possibly after earlier acknowledgements | `failed` | `failure`, with partial summary | 1 |
| Audit persistence failure with no unknown writes | `failed` if status persistence succeeds | may be incomplete; print known partial results | 1 |
| Any unknown write | `failed` | `outcome_unknown`, with partial summary | 3 |
| Metadata check succeeds, all expected names present | `executed` | `success` | 0 |
| Metadata check fails or expected names are missing | `failed` | `failure` | 1 |

Preflight failures leave plan status unchanged. Ordinary invalid-plan handling remains unchanged. An interrupted client or broker may not finish status/audit persistence; distinguish a missing response from evidence of no write.

Proposed user messages:

```text
OK GitHub write acknowledged: TARGET_NAME

GitHub write outcome unknown: TARGET_NAME
Do not retry automatically. Ask the operator to resolve the outcome first.

Sync stopped.
Acknowledged: 1
Unknown: 1
Not written: 0
Not attempted: 2
```

Print the corresponding planned source/target names when needed. Do not say "nothing was written" for an entire batch that has acknowledged or unknown entries. `failed` does not mean all targets were unchanged. Successful name verification does not prove a previous unknown write stored the intended value.

### Unknown-outcome exit codes across vault operations

Use one public rule: exit 3 if any write outcome is unknown; otherwise an unsuccessful execution exits 1. Apply it to GitHub writes, `vault.secret.generate`, `vault.secret.rotate`, `vault.secret.import_file`, and `vault.secret.copy_many`. `secretops add` already follows this rule and keeps it.

For vault partial writes handled by `_stop_partial_write`, an unconfirmed current write exits 3 even when earlier names were confirmed. A missing source or a vault error before the current name's write exits 1, even when earlier writes succeeded. Earlier confirmed writes do not turn a known later failure into an unknown result. Update `_fail_unknown_write` and the partial-write exit selection, retain existing vault result/audit semantics, and add process-level coverage for both partial cases. Do not redesign vault batch accounting in this PR.

## Retry and compatibility decisions

No automatic retry, rollback, or resume. In particular, do not rerun an unknown batch or implicitly resend its acknowledged prefix. Agent instructions must explicitly say: exit 3 or any reported unknown outcome means stop and notify the human; do not create a new sync plan until the human resolves the state. A human investigates uncertain entries, then authorizes preparing and approving a new explicit plan for work that should actually be sent. Terminal plan statuses remain non-executable as in the existing lifecycle; do not add a new replay path. This PR does not add concurrency locks or exactly-once guarantees for existing GitHub plans.

Contract decision: keep global plan contract version 1. Approval still covers the same source names, values obtained at execution, ordered mappings, target destination, and write mode. Conservative failure reporting, audit additions, and bounded execution do not authorize different data, destinations, or writes. Interrupted/partial execution was already possible; timeout handling makes that limitation explicit. These changes therefore do not require invalidating unrelated open plans. Digest canonicalization, prefix, saved-plan schema, and existing approvals remain unchanged. This does not weaken the existing versioning rule: a change to what an approved operation authorizes still requires a contract-version change.

The unified unknown-outcome exit code is a CLI compatibility change: scripts that assumed every execution failure exits 1 must handle exit 3 and stop for human investigation. Document this in README and broker upgrade/agent instructions. No automatic replanning or approval migration is needed for this PR.

## Implementation and documentation

- `targets/github_cli.py`: bounded calls, safe errors, explicit write outcomes, validated metadata response; preserve binary and child-environment isolation. Export needed types from `targets/__init__.py`.
- `plan_lifecycle.py`: uniform single/batch outcome handling, partial results across both vault and GitHub failures, failure/unknown messages, metadata errors, and safe audit events.
- Plan contracts: retain version 1 and existing schema/digest fixtures. Neutral outcome fixtures pin the new CLI/audit behavior without altering the saved authorization.
- Tests: fake CLI/process-level tests for public behavior, plus focused unit tests for classification and environment protection. Use synthetic values; test output must not contain them.
- README, BROKER_HOST, AGENT_USAGE, ARCHITECTURE, SECURITY_MODEL, TODO: timeout configuration, outcome/exit meanings, partial results, exit-code migration, and operator recovery. AGENT_USAGE must explicitly require stopping and notifying the human on exit 3 or any unknown outcome, with no new sync plan before human resolution. Keep unresolved interactive-entry validation limits visible.

## Acceptance criteria

Automated:

1. Every GitHub subprocess receives the configured timeout. Missing setting uses the existing default; invalid settings stop before reading a vault value or launching `gh`.
2. Single writes cover success, factory/launch failure, nonzero exit, signal exit, timeout, and unexpected post-launch communication failure. The latter four are unknown, with no automatic second invocation.
3. Metadata calls cover success, valid empty output, missing expected names, launch failure, timeout, nonzero exit, and malformed output. Failures never become confirmed missing names or unknown writes. An unrelated valid GitHub name such as `_FOO` does not fail verification of expected project names; malformed rows still fail without printing content.
4. A batch with acknowledgement, then unknown, then remaining work reports the correct ordered mapping categories, stops immediately, records partial progress, ends `failed`, and exits 3.
5. A batch with an acknowledged prefix followed by a vault read error/missing source or proven CLI launch failure preserves the prefix, records the current mapping as not written, skips the rest, and exits 1. Include a failure before any write and a fully successful batch.
6. No synthetic value appears in output, errors, plans, audit, command arguments, or child environment, including fake-provider output and timeout partial output. Environment/binary-selection regressions continue to pass. Audit-persistence failure stops subsequent work, prints the known in-memory partial summary and the persistence warning, and exits 3 only if a write is unknown, otherwise 1. An acknowledged write remains acknowledged. Include an error after a successful write and an error while recording an unknown result.
7. `verify_many` remains metadata-only and cannot resolve an unknown write by name presence. Preflight retains its no-GitHub-call behavior and does not claim authentication has been verified.
8. Existing valid version-1 plans and approvals remain usable; contract version, strict schema, and digest fixtures remain unchanged. Unknown vault generate/rotate writes and unknown partial import/copy writes exit 3; a partial vault failure before the current write exits 1 despite earlier confirmed writes. Assert both cases and retain existing no-leak/audit checks.
9. Process-level tests assert exit codes 0/1/3, plan states, safe audit categories and complete batch partitions without relying solely on internal enum assertions. The migrated factory/process tests retain exact-environment and binary/host checks, including the existing GitHub-isolation suite; test both sides of the launch boundary. Run the full pytest suite and wrapper checks before PR.

Manual after implementation and review:

10. Record the actual client/broker commit, wheel checksum, Infisical and `gh` versions. Upgrade the application without changing credentials, login, provider binaries, or SSH access; preserve existing plan compatibility and document exit-code migration and any deliberate policy setup.
11. On the tested local sudo path, enter a new synthetic `dev` secret through `secretops add`. Agent prepares/preflights a separate sync plan to an explicitly selected GitHub test environment; human approves; agent executes. A subsequent separate `verify_many` plan confirms the target name. Do not read or print provider values. Record that GitHub acknowledgement and metadata presence do not prove byte equality.
12. Record redacted results and exact limits in `docs/validation/`. Fault injection belongs in fake-provider tests; do not manipulate live credentials or provoke ambiguous live writes merely to test timeout handling. Human SSH entry and tmux remain unverified unless separately exercised.

## Review decisions

Review decisions: keep contract version 1; unify unknown-write exit code 3 across GitHub and vault execution (including partial writes); inject a `Popen` factory and preserve isolation assertions; distinguish provider metadata names from project naming policy; stop agents on unknown outcomes; show known partial results even when audit persistence fails. The per-call timeout/default and recovery behavior above apply without changing approved operation content. Initial design review used source inspection only; subsequent automated and live evidence is recorded in the [validation record](../validation/github-target-reliability.md).

## Implementation notes

- `GitHubCliEnvSecretTarget` takes `popen_factory` (default `subprocess.Popen`) and resolves the timeout at construction. `_start` launches `gh`; an `OSError`, `ValueError`, or `SubprocessError` there raises `GitHubLaunchError`. `_communicate` sends stdin and waits; a timeout kills `gh`, collects within 5 seconds, then closes the pipes and waits again within 5 seconds, so a child that inherited the pipes cannot hold the broker. Partial output from the timeout is dropped.
- `set_secret` returns `GitHubWriteOutcome`; `list_secret_names` returns names or raises a `GitHubError` subclass. `GitHubConfigurationError` is now a `GitHubError`.
- Listing rows must match the non-TTY format of `gh secret list --env`: a name, one tab, and the update time. gh v2.4.0 (`pkg/cmd/secret/list/list.go`) prints the date as `YYYY-MM-DD` and adds the `Updated` prefix only on a TTY; later versions print an RFC 3339 timestamp; environment secrets have no visibility column. Any other row (name only, extra fields, TTY format, leading space) is a response error. The name must match GitHub's rule `[A-Za-z_][A-Za-z0-9_]*`; expected names are compared case-insensitively.
- Ctrl+C while `gh` runs stops `gh` (`GitHubInterruptedError`): a write is then unknown, the batch stops and reports, and execution exits 3; a listing fails as a check error. Ctrl+C during the vault read for a mapping marks it not written (`interrupted`). Interruption at other points is not handled specially.
- If the plan status cannot be saved, the run prints the results and a warning not to run the plan again, and exits 1 (3 if a write is unknown), because the plan may still be `approved`.
- `write` and `sync_many` share one transfer routine. Per-mapping audit events carry `step: mapping`, `mapping_index`, target name and destination, `outcome`, and a `failure` category (`source_missing`, `vault_error`, `launch_failed`); the final event carries `step: summary` and the four lists of `{index, source, target}`. A vault error's safe message is printed but not audited.
- Vault: `_fail_unknown_write` exits 3; `_stop_partial_write` exits 3 for an unconfirmed current write and 1 otherwise.
- Tests: `tests/fake_process.py` (factory and process doubles), `tests/test_github_reliability.py` (adapter, process-level batch, audit failure, metadata, vault exit codes, a real subprocess timeout with a pipe-holding child); `test_github_environment.py` and `test_github_target.py` now use the factory and keep their exact-environment checks.
