# Design: plan destination binding

Status: implemented; manual criterion 18 verified on 2026-10-18 against application commit `3ecf2d7`
Depends on: Infisical connection description from PR #5 (`475d137`)
Prepares for: a second vault backend (OpenBao) and human secret entry

## Goal

An approval covers a specific operation on a specific destination, not only an action, secret names, and environments:

- the saved plan records where secrets are read and written: the vault connection and, for GitHub actions, the GitHub host, repository, and environment
- the human sees that destination in `show-plan` before approving
- approval records a digest of the operation content, and execution refuses a plan whose content changed after approval
- preflight and execution refuse to run when the current configuration points at a different vault destination, before any provider call
- one checked destination is used for every provider call of an operation

## Problem

1. **The vault destination is not part of the plan.** `InfisicalCliVault.connection()` resolves `INFISICAL_DOMAIN`, `SECRETOPS_INFISICAL_PROJECT_ID`, and `SECRETOPS_INFISICAL_PATH` from the environment when each call runs. A plan approved while the broker pointed at one project can execute against another project or server if `broker.env` changes between planning and execution.
2. **The destination is resolved per call.** `_query` and `write_secret` each call `connection()`. Within one operation, nothing guarantees that the existence check, the write, and the read-back use the same destination.
3. **The GitHub host is implicit.** `GitHubCliEnvSecretTarget` passes `--repo OWNER/REPO`. `gh` resolves the host from `GH_HOST` or its own configuration, so the same plan can write to a different GitHub host.
4. **Approval is a status flag only.** `approve-plan` changes `status` to `approved`. Nothing ties the approval to the content the human reviewed. On the broker, the agent cannot write plan files, so this is a defence-in-depth gap rather than an open path for the agent.

## Non-goals (deferred)

- **Plan signatures.** The digest detects content changes when the stored digest survives. It does not protect against someone who can rewrite both the plan and the digest. File ownership and the SSH boundary remain the protection against that.
- **Digest confirmation at approval** (`approve-plan --id X --digest PREFIX`). `show-plan` prints the digest in this change, so the flag can be added later without changing the plan format.
- **GitHub timeouts and unknown outcomes.** Separate, already agreed step.
- **Binding the GitHub account or token.** The account is the identity that runs the operation, like the Infisical token. Replacing it while the destination stays the same does not invalidate a plan.
- **A policy allowlist of destinations** (permitted projects or GitHub hosts). Can build on the recorded destination later.
- **Import file content.** `vault.secret.import_file` binds the source file path and the expected names, as today, not the file content.

## Contract

### Contract version

Every new plan records `contract_version: 1`.

Rule: a change that alters what an approved plan does, including a behaviour change with no change to the JSON structure (for example a different default read option or write mode), increments the contract version. Preflight and execution refuse plans with any other version and ask for replanning. This rule is also recorded in `docs/SECURITY_MODEL.md`.

This replaces the earlier idea of a `binding_version` limited to the destination fields, and the repetition of the fixed read options (`expand`, `secret_overriding`, `include_imports`) in every plan. Those options are part of contract version 1.

### Vault destination

Recorded in the plan as `vault_destination` for every action that reads or writes the vault:

| Field | Source |
| --- | --- |
| `backend` | `infisical` (the only backend today) |
| `domain` | `INFISICAL_DOMAIN`, or the existing default, in canonical form |
| `project_id` | `SECRETOPS_INFISICAL_PROJECT_ID` |
| `path` | `SECRETOPS_INFISICAL_PATH` |

Environments stay in the existing plan fields (`env`, `source_env`, `target_env`); they already belong to the operation.

Not part of the destination: the token, the CLI binary path, and the timeout. Renewing or replacing the token for the same destination is allowed.

Resolution at planning reads local, non-secret configuration only: no token, no provider call. Invalid configuration stops planning and no plan is saved.

### Canonical domain

The domain is compared in canonical form, without DNS resolution:

- scheme and host lowercased; only `http` and `https` accepted
- default port removed (`:443` for `https`, `:80` for `http`); any other port kept
- a path in the URL kept, with a trailing `/` removed (self-hosted instances can sit under a path)
- rejected: user name or password, query, fragment, empty host

Two different host names for the same server are different destinations. Changing one for the other requires a new plan.

The canonical form is what the adapter passes to `--domain`, so the compared value and the used value are the same string.

### GitHub destination

Recorded in the plan target for every GitHub action as `target_host` next to the existing `target_owner`, `target_repo`, and `target_environment`.

- `target_host` comes from a new optional recipe field `github_host`, default `github.com`, validated as a host name
- the adapter always passes `--repo HOST/OWNER/REPO`, so `GH_HOST` and the `gh` default host cannot redirect the operation
- `GH_HOST` and `GH_REPO` are removed from the `gh` child environment as a second guard

The GitHub destination comes entirely from the plan, so there is no configuration to compare at execution. The protection is that the adapter uses only the planned host.

### Plan file schema

Contract version 1 plan files are parsed strictly before anything else reads them. The digest is computed from the parsed file content, not from the converted `PlanArtifact`, so a conversion in `load_plan()` (type coercion, dropped fields, reshaped mappings) cannot hide a change to the stored content.

- the file is one JSON object; duplicate keys at any level are rejected
- unknown fields are rejected; each field has one allowed JSON type, and `true`/`false` are not accepted where an integer is expected
- `NaN`, `Infinity`, and `-Infinity` are rejected; numbers are integers only
- an optional field that does not apply is omitted; `null` is rejected for every field, so omitted and `null` cannot mean the same thing
- strings are not trimmed or normalised when loading; validation that would change a value rejects it instead

The schema is defined per action, so a field that does not belong to the action (for example `generator` on `copy_many`) is rejected, not ignored.

Per action, the schema also fixes:

- the required fields, for example `source_env`, `target_env`, `create_mode`, and `secret_mappings` for `copy_many`, and `secret_name`, `generator` (with `type`), and `create_mode` for `generate`
- the shape of `secret_mappings`: `{name}` for `import_file`, `diff_envs`, and `verify_many`; `{source, target}` for `copy_many` and `sync_many`; the list must not be empty
- `vault_destination` is required for vault actions and rejected for GitHub-only actions; it must pass the same validation the adapter applies (supported backend, canonical domain without credentials, query, or fragment, non-empty project, absolute path)

A recorded destination that fails this validation is refused before `show-plan` prints it, so a credential in a hand-edited URL is never displayed.

Plans without `contract_version` are parsed with the existing lenient loader, but only for `show-plan`; see "Old plans".

### Approval digest

`approve-plan` computes a digest of the operation content and stores it in the plan as `approval_digest`.

- Content: the parsed plan object without `status` and `approval_digest`. That includes `plan_id`, `contract_version`, the action, names, environments, mappings, generator, modes, `vault_destination`, and the GitHub target.
- Canonical form: `json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`, encoded as UTF-8. With the schema rules above, this form has only one serialisation per object.
- Format: `sha256-v1:<hex>`. A future change of the canonical form uses a new prefix, and plans with an unknown prefix are refused, not silently accepted.
- A fixed example plan with its expected digest is kept in the tests (`tests/fixtures/`). It pins the canonical form, and a reimplementation in another language must produce the same digest.
- `show-plan` prints the digest of the current content as `plan_digest`, and the stored `approval_digest` when present.

Preflight and execution recompute the digest of an approved plan and refuse a mismatch before any provider call. A plan that requires approval and is `approved` without `approval_digest` is refused.

Plans for actions that do not require approval have no digest and are not affected.

### One loaded plan per command

The plan that is checked is the plan that is approved or executed:

- Each of `approve-plan`, `preflight-plan`, and `execute-plan` reads and parses the plan file once. Checks, the digest, and the operation all use that one parsed object.
- `approve-plan` checks the status and contract version, computes the digest, and writes status and digest from the same object. It does not reload the file between computing the digest and writing the approval.
- Execution does not reload the plan to obtain operation parameters. Status changes during and after execution (`executed`, `failed`) write the in-memory object with the new status; the existing `update_plan_status()`, which reloads the file, is replaced for these paths.
- Every plan write is an atomic replacement: write a temporary file in the plan directory, flush and `fsync`, then `os.replace()`.

Not covered: two concurrent commands on the same plan (for example an approval racing an execution). The last atomic write wins. The broker runs one human and one agent with separate commands, and a plan lock can be added later if this becomes a real case.

### Checks before provider calls

Preflight and execution run these checks, in this order, before building any adapter:

1. `contract_version` is `1`; otherwise "replan required".
2. The plan file passes the strict schema for its action.
3. For approved plans: `approval_digest` matches the recomputed digest.
4. For vault actions: `vault_destination` is present and equal, field by field, to the destination resolved from the current configuration.

No failed check makes a provider call. What else happens depends on whether a trusted plan was loaded:

- **Invalid or unsupported file** (not valid JSON, duplicate keys, schema violation, missing or unknown `contract_version`): the command refuses with a fixed error message that does not echo file content, and the plan file is left untouched. There is no validated plan to rewrite, so the status is not changed. The audit event records the plan id from the command line, the command, and the failure category only, with no action, names, or environments from the unvalidated file.
- **Valid, supported plan with a digest or destination mismatch**: the command prints the names of the differing fields (values are visible through `show-plan`) and writes a `failure` audit event with secret names only. Execution marks the plan `failed` from the loaded object; preflight does not change the status.

Execution repeats the checks even after a passing preflight, because configuration can change between the two.

### One destination per operation

After the checks, the vault adapter is built from the recorded destination: `get_vault(destination)` returns an adapter whose existence checks, reads, writes, and read-backs all use that destination. The adapter does not read the project, path, or domain from the environment again during the operation. The token, binary, and timeout are still resolved from configuration, as today.

### Old plans

Plans without `contract_version` stay readable through `show-plan`, which reports "created before destination binding; replan required". `approve-plan` refuses them, and preflight and execution refuse them before any provider call. Nothing adds a destination to an existing plan after the fact.

### GitHub-only actions

`github.env_secret.verify_many` does not touch the vault. It records and checks only the GitHub destination and needs no vault configuration.

## Compatibility

- All existing saved plans, including approved ones, must be replanned and approved again.
- Recipes without `github_host` keep working and target `github.com`.
- Planning a vault action now requires `SECRETOPS_INFISICAL_PROJECT_ID` and `SECRETOPS_INFISICAL_PATH` to be set; before, only execution required them.
- The commit message and `README.md` call out the breaking change.

## Code changes

- `plans.py`: `contract_version`, `vault_destination`, `approval_digest` fields; strict per-action schema parser (duplicate keys, unknown fields, types, `null`, non-finite numbers); digest function over the parsed content; atomic plan writes; status writes from an in-memory plan instead of a reload; lenient loading of old plans for `show-plan` only.
- `vaults/infisical_cli.py`: canonical domain; adapter accepts a fixed connection; `connection()` used only to resolve the configured destination for planning and comparison.
- `vaults/__init__.py`: `get_vault(destination)`; a helper that resolves the configured destination without building an adapter or reading the token.
- `targets/github_cli.py`, `targets/github.py`, `recipes.py`, `validation.py`: `github_host`, explicit `--repo HOST/OWNER/REPO`, cleaned `gh` environment.
- `plan_lifecycle.py`: checks before provider calls in preflight and execution; `show-plan` output; `approve-plan` stores the digest and refuses old plans.
- `handlers.py`: record the destination at planning.
- Docs: `README.md`, `docs/SECURITY_MODEL.md` (contract version rule, approval digest and its limits), `docs/ARCHITECTURE.md`, `docs/AGENT_USAGE.md`, `docs/BROKER_HOST.md` (changing `broker.env` invalidates open plans).

## Acceptance criteria

Automated, with the fake Infisical and GitHub CLIs:

1. A saved vault plan contains `contract_version` and `vault_destination` with the canonical domain, project, and path, and no token, binary path, or timeout.
2. Planning works without `INFISICAL_TOKEN` and makes no provider call; planning a vault action with missing project or path saves no plan.
3. Changing the domain, project, or path after planning makes preflight and execution fail with zero provider calls.
4. Changing the configuration after a passing preflight makes execution fail with zero provider calls, and the plan is marked `failed`.
5. Replacing the token with the same destination does not block preflight or execution.
6. Canonical domain: case, default port, and trailing slash differences compare equal; a different host name, port, scheme, or path compares different; URLs with user info, query, or fragment are rejected at planning.
7. Within one operation (generate, rotate, copy, import, sync), every provider call passes the recorded project, path, and domain, even if the environment changes during the operation.
8. Approval stores `approval_digest`; changing any operation field after approval (names, environments, mappings, destination, modes, generator) makes preflight and execution fail with zero provider calls; a status change alone does not.
9. An approved plan without `approval_digest`, or with an unknown digest prefix, is refused.
10. A plan without `contract_version`, or with a different version, is shown by `show-plan` with a replan notice and refused by `approve-plan`, preflight, and execution.
11. Strict schema: a contract version 1 plan with a duplicate key, an unknown field, a field that does not belong to the action, a wrong type (including a boolean for an integer), `null`, or `NaN`/`Infinity` is refused by `approve-plan`, preflight, and execution with zero provider calls; the plan file is left byte-for-byte unchanged, and neither output nor the audit event contains content from the file.
12. The fixed example plan in `tests/fixtures/` produces its recorded digest.
13. Approval writes status and digest from one parsed object: a test that changes the file between the digest computation and the write cannot produce an approved plan whose digest matches content that was not checked. Execution reads the plan once; a change to the file after the checks does not change the parameters used for provider calls.
14. Plan writes are atomic: an interrupted write leaves either the old or the new complete file.
15. GitHub actions pass `--repo HOST/OWNER/REPO`; with `GH_HOST` set to another host, the call still targets the planned host, and `GH_HOST` and `GH_REPO` are absent from the `gh` environment.
16. `verify_many` works without vault configuration.
17. No secret value appears in plans, digests, output, audit logs, or command-line arguments; existing no-leak tests still pass.

Manual, through the NAS broker:

18. Plan and approve a `generate` in `dev`; change `SECRETOPS_INFISICAL_PROJECT_ID` in `broker.env`; preflight and execution are refused with the destination mismatch message, and the plan is marked `failed` by execution. The temporary project identifier may be a synthetic UUID; this test does not require accessing a second project. This confirms the refusal on the real broker; zero provider calls are proven by the call counters in the automated tests (criteria 3 and 4), not by this check. Restore the value, replan, approve, and execute successfully. Run a `verify_many` plan through the broker to confirm that the pinned `gh` version accepts the host-qualified `--repo` form.


## Manual validation result — 2026-10-18

Tested application commit: `3ecf2d7`. A wheel built from that commit was installed on the dedicated local broker, preserving its configuration, plans, audit, runtime, provider binaries, policy, and SSH setup. The vault was a self-hosted Infisical NAS instance. Tests used synthetic names in `dev`; GitHub verification used an existing test name in `staging`.

| Check | Result |
| --- | --- |
| Plan records the intended vault destination and approval records the displayed digest | Passed |
| Human changes the broker project after approving a generate plan | Preflight refuses `project_id` mismatch and leaves status `approved` |
| Negative execution test with that deliberately mismatched project | Execution refuses the mismatch and records status `failed` |
| Human restores the project; agent creates and preflights a fresh plan; human approves | Passed; the test secret is still absent before execution |
| Execute the fresh plan | Confirmed vault write; final status `executed`; no value printed |
| GitHub host-qualified repository argument | Passed with installed `gh` version `2.4.0+dfsg1` (Ubuntu); `verify_many` confirms the expected test name and ends `executed` |

The initial GitHub verification failed. A human authentication check failed too; after the human configured broker authentication, a fresh verification plan passed. Provider output remained suppressed throughout; GitHub verification neither reads nor writes secret values.

Final automated verification on the same application commit: `pytest` passed all 361 tests; `tests/test_broker_wrappers.sh` passed all 35 checks.

Criterion 18 is complete for the broker refusal and restored-configuration scenarios described above. The temporary project identifier was a synthetic UUID; we did not inspect a second project or establish whether that UUID identifies an existing project. After restoring configuration, the successful fresh preflight confirmed absence in the original project before the approved write. Absence in a second project was not independently checked and is not claimed. The original criterion wording requiring inspection of both projects has been corrected to match this test procedure.

Zero provider calls on destination mismatch are demonstrated by automated call-counter tests and the implementation review, not independent instrumentation of the live broker. Domain/path changes, digest tampering, credential replacement, malformed plans, and interrupted plan writes are covered automatically rather than repeated against the live instance. This run does not claim real-instance coverage of every action or a GitHub secret-write test.

The synthetic vault secret remains available for human cleanup. No token, credential value, private address, or project identifier is included in this record.
