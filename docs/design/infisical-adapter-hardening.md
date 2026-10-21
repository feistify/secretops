# Design: Infisical adapter hardening

Status: implemented; acceptance criterion 14 verified on 2026-10-17 against Infisical CLI v0.43.99, with the limits listed under the result
Depends on: vault selection through `get_vault()` merged in PR #4 (`481fd21`)
Prepares for: plan-to-vault binding and a second vault backend (OpenBao)

## Goal

Make the existing Infisical adapter trustworthy as the source of truth for secret operations:

- "exists" and "missing" are proven answers from the vault, never a side effect of an error or of the broker's own environment
- values read for copy and sync are the stored vault values, never local variables or expanded references
- Infisical access uses explicit machine authentication and an explicit project and path, never an implicit user session or a project picked up from the working directory
- provider calls cannot block forever, and an interrupted write reports an unknown outcome instead of success or failure

This is a security fix of the current product. It is justified without a second backend.

## Problem

Findings from review of `src/secretops/vaults/infisical_cli.py` and the upstream Infisical CLI (`Infisical/cli`, `packages/cmd/run.go`):

1. **Broker environment leaks into vault answers.** The adapter runs `infisical run -- python3 -c "os.getenv(NAME)"` without passing `env=`. `formatSecretsForShell` in the CLI starts from `os.Environ()` and then adds vault secrets. A variable with the same name in the broker environment makes `secret_exists` return true and makes `read_secret_value` return the local value, which `sync_many` can then write to GitHub as if it came from the vault.
2. **Reserved names are silently dropped.** The CLI removes vault secrets named `HOME`, `PATH`, `PS1`, `PS2`, `PWD`, `EDITOR`, `XAUTHORITY`, `USER`, `TERM`, `TERMINFO`, `SHELL`, `MAIL`, or prefixed `XDG_` or `LC_`, with only a warning. The child then sees the broker's own value under that name.
3. **Errors look like missing secrets.** `secret_exists` returns false for any non-zero exit status, so authentication failure, missing permission, or an unreachable service look like "secret does not exist". `generate` and `import_file` treat that as permission to write.
4. **Empty values look like missing secrets.** The check uses the truthiness of `os.getenv(...)`, so an existing secret with an empty value is reported as missing and can be overwritten by `generate`.
5. **Implicit authentication.** Without `INFISICAL_TOKEN`, the CLI uses the logged-in user session. Upstream, the user-session path keeps a local encrypted backup and can return previously fetched secrets when the service is unreachable, so a "missing" answer can come from stale data. The service token and machine identity token paths call the API without that fallback.
6. **Implicit project and path.** The adapter passes neither `--projectId` nor `--path`. The project comes from `.infisical.json` or `.infisical.yaml` in the working directory, and the path defaults to `/`.
7. **Implicit value transformation.** `--expand` (secret reference expansion), `--secret-overriding` (personal overrides), and `--include-imports` are enabled by default. A read can return an expanded, personal, or imported value instead of the stored shared value at the configured path.
8. **No timeouts.** Provider calls have no timeout. A hung CLI blocks the broker indefinitely and keeps the temporary write file on disk until the call returns.

All findings predate PR #4.

## Non-goals (deferred)

- **Token delivery, renewal, and revocation.** This change requires a token and fails safely without one. How the broker obtains short-lived tokens (for example by exchanging Universal Auth client credentials per run) is a separate broker setup design.
- **Plan-to-vault binding (subsequently completed).** This was outside the hardening PR. PR #6 later added destination recording and checks at preflight and execution; see [plan-destination-binding.md](plan-destination-binding.md), including the 2026-10-18 broker validation.
- **Atomic conditional writes.** `create_only` and rotation still check existence and then write in separate calls. The Infisical CLI offers no conditional write. This limitation is documented, and conditional writes become part of the vault contract together with the OpenBao adapter.
- **GitHub adapter timeouts.** Separate change, same unknown-outcome rules.
- **Import support.** `include-imports` stays off. It can return later as a deliberate connection setting when there is a concrete need.

## Security contract

### Connection description

The adapter is built from one resolved, non-secret connection description, used identically for existence checks, reads, and writes:

| Field | Source | Required |
| --- | --- | --- |
| `domain` | `INFISICAL_DOMAIN` (existing default kept) | no |
| `project_id` | `SECRETOPS_INFISICAL_PROJECT_ID` | yes |
| `path` | `SECRETOPS_INFISICAL_PATH` | yes |
| `expand` | fixed `false` (reads only) | — |
| `secret_overriding` | fixed `false` (reads only) | — |
| `include_imports` | fixed `false` (reads only) | — |
| `secret_type` | fixed `shared` (writes only) | — |
| `binary` | `SECRETOPS_INFISICAL_BIN`, otherwise `/usr/local/bin`, `/usr/bin`, `/bin` only | no |

Missing `project_id` or `path` is a configuration error raised before any provider call.

The CLI subcommands accept different flags, so the contract is split by call type. Flag sets below are from the upstream CLI source (`packages/cmd/run.go` and `packages/cmd/secrets.go`) and must be confirmed against the pinned version.

| Call | Command | Flags |
| --- | --- | --- |
| existence check, read | `infisical run` + helper | `--domain`, `--projectId`, `--path`, `--env`, `--expand=false`, `--secret-overriding=false`, `--include-imports=false` |
| write | `infisical secrets set --file=<file>.yaml` | `--domain`, `--projectId`, `--path`, `--env`, `--type=shared` |

The token is passed only through the `INFISICAL_TOKEN` environment variable, never through the `--token` flag, which would put it on the command line.

### Write file format

The value is written to a `0600` temporary file ending in `.yaml`, holding one mapping `"NAME": "value"`, and passed with `--file`. The first NAS check (criterion 14) showed why `NAME=@<file>` is not used: in CLI v0.43.99 only the user-session branch of `secrets set` expands `@<file>`; with a machine token the CLI passes the raw arguments on and stores the literal `@<path>`. Read-back caught it and the write was reported as `outcome_unknown`.

- The `.yaml` suffix selects the YAML parser. Other names go through the .env parser, which strips surrounding whitespace and quotes.
- The file is written with `yaml.safe_dump` with every scalar double-quoted and no line folding. The CLI parses with Go `yaml.v3` (YAML 1.2), where plain scalars such as `0o17`, `true`, or `null` are not strings; double-quoting keeps every value a string for both parsers.
- `--file` rejects empty and whitespace-only values (and the argument form rejects empty ones). The adapter refuses such a value before the CLI starts, so the write fails rather than ending in an unknown outcome. An existing empty secret is still read as present with value `""`.
- The fake CLI in the tests mirrors the machine-token behaviour (`tests/fake_infisical.py`), but it parses YAML with PyYAML. YAML 1.2 compatibility is confirmed only by the real CLI and read-back.

### Authentication

- `INFISICAL_TOKEN` (machine identity or service token) is required for every operation that contacts Infisical. There is no fallback to a user session and no opt-in to allow one.
- The token is required lazily, when the adapter is about to call the CLI, not when the adapter is created. Planning, `show-plan`, and operations that contact only GitHub (for example `github.env_secret.verify_many`) do not need it. Operations that contact Infisical include `check-vault-read`, `execute-recipe` for `vault.read.check`, and every `preflight-plan` and `execute-plan` path that reads or writes the vault.
- A missing token stops the operation with a safe error before the CLI runs. A token the provider rejects is reported as an authentication error, distinct from "secret does not exist", and never triggers an interactive login.
- One credential per broker. The broker identity is not copied to a laptop. A human runs vault operations through the broker, or gets a separate identity if independent local access is needed.

### Child process environment

Every Infisical CLI call runs with an explicit environment instead of the inherited one:

- `INFISICAL_TOKEN`
- `PATH` set to a fixed value controlled by secretops, not the caller's `PATH`
- `HOME` set to a fresh, empty temporary directory with `0700` permissions, removed after the call
- optional TLS trust settings (`SSL_CERT_FILE`, `SSL_CERT_DIR`) passed through only if set, for self-hosted instances with an internal CA
- nothing else; in particular no `DBUS_SESSION_BUS_ADDRESS`, `XDG_*`, or other `INFISICAL_*` variables

### Helper process

The minimal environment above applies to the CLI. The helper child started by `infisical run` additionally receives every secret at the configured path as an environment variable, including unrelated ones. The helper must not be influenced by them:

- it runs with the absolute path of the current interpreter (`sys.executable`), not `python3` from `PATH`
- it runs in isolated mode (`-I`), which ignores `PYTHON*` environment variables and keeps the script directory, the current directory, and user site-packages out of the module path; an unrelated vault secret such as `PYTHONPATH` or `PYTHONHOME` must not change how the helper loads or starts
- it checks its own environment for dynamic loader variables (`LD_` prefix) and reports a provider error if any are present, instead of answering; this is detection, not a guarantee (see below)

`-I` does not cover every variable that affects a process. Loader variables such as `LD_PRELOAD` act before the helper runs. If unwanted code already ran, the helper's own check is no longer a reliable security boundary, so it is kept only as additional detection. This scope explicitly accepts the residual risk: a vault path that contains loader variables together with a matching file on the broker host. Keep application secrets at a path that holds only those secrets. Full prevention needs a mechanism that acts before the helper starts and is out of scope here.

This is not full process isolation. It closes the usual paths to a user session, user configuration, the keyring, and local variables. The guarantee comes from the combination of a required token, the controlled environment, verified behaviour of a pinned CLI version, and tests that prove there is no fallback.

### Response protocol

This protocol applies to existence checks and reads, which run through the helper. Writes have their own success criterion, described under "Write outcomes".

The helper child prints exactly one line of JSON to stdout and exits 0. Each call passes a random nonce as an argument, and the response must echo it:

```json
{"nonce": "<nonce>", "present": false}
{"nonce": "<nonce>", "present": true}
{"nonce": "<nonce>", "present": true, "value": "<value>"}
```

The adapter accepts a response only if all of these hold: exit status 0, stdout is exactly one line, the line parses as the expected JSON shape, and the nonce matches. Anything else, including extra stdout, a missing nonce, or a non-zero status, is a provider error. Matching the word "absent" in the output is not sufficient.

Presence is tested with `NAME in os.environ`, so an empty value counts as present.

The response with a value stays inside the adapter. It is never printed, logged, written to a plan, or included in an error message.

### Results instead of booleans

- Existence returns one of: `present`, `missing`, or an error. Only a well-formed `missing` response means the secret is absent.
- Read returns one of: a value (possibly empty), `missing`, or an error. "Missing" and "present with an empty value" are distinct.
- Errors are raised as typed adapter errors with safe categories: configuration, authentication, provider unavailable, timeout, malformed response. Provider stdout and stderr are never shown.
- Every operation that writes (generate, rotate, import_file, copy_many, sync) stops on an existence or read error. An error never counts as permission to create.

`missing` means absent from the successfully fetched, authorized set of shared secrets at the configured project, environment, and path, with imports and personal overrides disabled. It makes no broader claim.

### Reserved names

Secret names are rejected if they are:

- in the Infisical CLI reserved list: `HOME`, `PATH`, `PS1`, `PS2`, `PWD`, `EDITOR`, `XAUTHORITY`, `USER`, `TERM`, `TERMINFO`, `SHELL`, `MAIL`
- prefixed `XDG_` or `LC_`
- a variable secretops passes to the child: `INFISICAL_TOKEN`, `SSL_CERT_FILE`, `SSL_CERT_DIR`
- prefixed `INFISICAL_`, `SECRETOPS_`, `PYTHON`, or `LD_`

The check runs at planning time for early feedback and again before every provider call, so an existing or hand-edited plan gets the same validation.

### Timeouts

- Every Infisical CLI call has a timeout (`SECRETOPS_PROVIDER_TIMEOUT_SECONDS`, default 60).
- A timeout on an existence check or read is an error and stops the operation.
- The temporary write file is removed in all cases, including timeout.

### Write outcomes

A write has three possible outcomes, and only the first counts as success:

- **confirmed:** the CLI exited 0 and a read-back through the helper returns a value equal to the written value. The comparison happens in memory inside the adapter; neither value is printed or logged.
- **failed:** secretops stopped before the CLI started (configuration error, missing token, rejected name, empty or whitespace-only value). Nothing was sent to the provider.
- **unknown:** every unconfirmed write after the CLI started. This includes a non-zero exit, a timeout, a dropped connection, a CLI crash or signal, a read-back that returns `missing` or a different value, and a read-back that itself fails.

Once the CLI has started, neither its exit status nor a read-back can prove the write did not happen: the request may still be processed on the server after a read-back already returns `missing`. An unconfirmed write is therefore never reported as a confirmed failure.

On `unknown`, the plan is marked `failed`, the audit event records `outcome_unknown` with the secret name and target environment of that write (and, for import and copy, `confirmed_names` and `not_written_names`), and the message says the write was not confirmed and the secret must be checked before any retry. secretops never retries a generate or rotate automatically.

Read-back verifies the stored value, not that no other writer touched it in between. Concurrent writers remain out of scope until conditional writes exist.

## Compatibility

This is a breaking change for current setups:

- setups that rely on `infisical login` stop working until `INFISICAL_TOKEN` is set
- setups that rely on `.infisical.json` in the working directory stop working until `SECRETOPS_INFISICAL_PROJECT_ID` and `SECRETOPS_INFISICAL_PATH` are set
- secrets that are only visible through imports or personal overrides become `missing`
- values containing secret references are read unexpanded
- secret names in the reserved list are rejected

`README.md`, `docs/SECURITY_MODEL.md`, `docs/BROKER_HOST.md`, and `docs/AGENT_USAGE.md` are updated in the same change, including the note that a human runs vault operations through the broker or with a separate identity. The commit message calls out the breaking change.

## Code changes

- `vaults/infisical_cli.py`: connection description, explicit environment builder, isolated helper with response protocol and nonce, typed results and errors, timeouts, write read-back, lazy token check.
- `vaults/base.py`: return types for existence and read that distinguish present, missing, and empty, plus the adapter error types. `write_secret_value` is replaced by `write_secret`, which returns confirmed or unknown; a write that fails before the CLI starts raises instead. The create vs. replace distinction is unchanged in this step.
- `validation.py`: reserved-name rules shared by planning and execution.
- `plan_lifecycle.py`, `handlers.py`, `cli.py`: handle the new results; stop on errors; record `outcome_unknown` for unknown write outcomes.

## Acceptance criteria

Automated, with a fake Infisical CLI:

1. A broker variable with the same name as a missing vault secret yields `missing` from the existence check and `missing` from the read.
2. A local variable value never reaches the GitHub target adapter during `sync_many` or `write`.
3. A missing `INFISICAL_TOKEN` stops the operation before the CLI runs and never starts a login or uses a user session.
4. The child environment contains only the allowlisted variables, with a fresh temporary `HOME`.
5. Every existence check and read passes `--projectId`, `--path`, `--expand=false`, `--secret-overriding=false`, and `--include-imports=false`. Every write passes `--projectId`, `--path`, and `--type=shared`. No call passes `--token` or any secret value on the command line.
6. Authentication failure, unreachable service, malformed output, extra stdout, wrong nonce, and non-zero exit are each reported as errors, never as `missing`, and stop every writing operation.
7. An existing secret with an empty value is `present`, and `generate` with `create_only` refuses to overwrite it.
8. Reserved names are rejected at planning and again at execution for a hand-edited plan.
9. A timeout on read or existence stops the operation.
10. Write outcomes: exit 0 with a matching read-back is confirmed; an error before the CLI starts is failed; every other outcome after the CLI starts, including a non-zero exit with a `missing` read-back, is `outcome_unknown`, and nothing is retried.
11. Unrelated vault secrets `PYTHONPATH` and `PYTHONHOME` at the configured path do not change the helper's answer. With a fake CLI that injects an inert `LD_`-prefixed variable, the helper reports a provider error instead of an answer. This tests the detection only; it does not claim protection against a loader variable that already took effect.
12. Planning and GitHub-only operations work without `INFISICAL_TOKEN`.
13. Existing no-leak tests still pass, and no test value appears in output, audit logs, plan artifacts, or command-line arguments.

Manual, against a real instance:

14. On the NAS Infisical instance, with a pinned CLI version recorded in the result: the response protocol works end to end; a broker variable does not leak into answers; no user session or local backup is used when the token is missing or invalid; CLI warnings do not reach the child's stdout; `--expand=false`, `--secret-overriding=false`, and `--include-imports=false` behave as described on `run`; `secrets set --file` with a machine token stores the file content byte-exact, confirmed by read-back, and accepts `--type=shared` and the flag set assumed above. Fake CLI tests do not replace this check.

### Criterion 14 result

Verified on 2026-10-17 on the NAS broker: secretops `9148c9b` installed as a wheel, Infisical CLI v0.43.99, self-hosted server `infisical/infisical:v0.161.10`. All checks used `NASCHECK_*` test names in `dev` and `staging`, with human approval of every plan.

The first run found that the CLI stores `NAME=@<file>` literally when it uses a machine token. Read-back reported `outcome_unknown` and nothing was retried; the write path was changed to `--file` (see "Write file format").

Passed:

- generate (`create_only`), rotate, and copy each confirmed by read-back; a repeated create-only preflight refused the existing target
- a broker-local variable with a secret's name did not make the secret present
- missing token: configuration error before the CLI started; invalid token and unreachable server: provider errors, never "missing"
- `--expand=false`: a copied secret reference stayed literal in the target
- `--include-imports=false`: a secret present only through a linked folder was reported missing
- audit log: all 38 events carried names and metadata only

Not established:

- Reuse of a stored `infisical login` session was not tested on a real instance; no session was provisioned on the broker. It is ruled out by design: without a token the CLI is not started; with a token the CLI takes the token branch; the CLI gets an empty temporary `HOME` and no D-Bus session. The invalid-token check failed closed.
- `--secret-overriding=false` was not isolated. A personal override added in the UI did not reach the copied value, but a machine identity does not see a person's overrides anyway.
- Real-CLI byte-exactness was shown for generated passwords and a reference string only, not for the YAML edge cases in the unit tests (such as `0o17`, `true`, or multi-line values). A parser mismatch would fail closed: read-back would report `outcome_unknown`.
