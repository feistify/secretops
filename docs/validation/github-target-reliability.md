# GitHub target reliability validation

Date: 2026-10-18
Tested client and broker application: commit `d0d17aa`
Broker wheel SHA-256: `27b3e71dba28162a6ffc395b38d1169714b484ec876f6fec74ecd12a9e052699`
Infisical CLI: `0.43.99`, confirmed by the human version-only check earlier in this session; the upgrade did not replace the provider binary.
GitHub CLI host package: `2.4.0+dfsg1-2`, confirmed during this run through package-manager metadata. The earlier human CLI version check reported `gh 2.4.0+dfsg1`; no new direct `gh --version` invocation or executed-binary tracing was performed for this run.

## Setup

A human installed the built application on the dedicated local broker. Isolated bundle checks and temporary-directory installation/rollback checks passed before the live upgrade. The previous application was retained for recovery. Credentials, stored GitHub login, plans, audit, policy, runtime, provider binaries, wrappers, and SSH configuration were preserved. Plan contract version remained 1, and no existing approval migration or blanket replanning was required.

The already-configured policy permitted human `vault.secret.add` in `dev` and separately approved GitHub sync to the test repository's `staging` environment. No policy change was made for this upgrade. The client used the previously tested local sudo route. The agent used only restricted SSH broker commands to upload metadata recipes, plan, show, preflight, and execute; human approval remained separate.

## Complete workflow results

The source and target name was `NASCHECK_GH_RELIABILITY_D0D17AA`. All input was synthetic; no production operation was performed.

| Step | Result |
| --- | --- |
| Human runs `secretops add` for the new name in vault `dev` | Destination displayed; hidden input and human confirmation completed; client reported `Saved and verified.` |
| Agent creates a separate `github.env_secret.sync_many` plan | One explicit mapping from the vault name to the same GitHub name in a private test repository, environment `staging` |
| Agent preflights the sync plan | Passed; source present; no GitHub API call during preflight |
| Human approves the sync through their own broker access | Approval succeeded and recorded the reviewed digest |
| Agent executes the approved sync | Exit 0; `OK GitHub writes acknowledged: 1`; no value printed |
| Agent prepares and preflights a separate `github.env_secret.verify_many` plan | Passed for the one expected target name |
| Agent executes metadata verification | Exit 0; one expected name present, no expected names missing |
| Human inspects GitHub environment secrets in the web UI | Confirms the new test name appears in `staging` |
| Agent checks saved plan metadata after execution | Both sync and verification plans show `executed`; sync approval/current digests match; `secret_value_included` is false |

Vault `Saved and verified` means exact internal read-back through the Infisical adapter. GitHub `acknowledged` means the CLI exited successfully. The subsequent broker verification and human UI check establish name presence only; neither proves byte-exact equality of the GitHub value with the source. No GitHub secret value was read back or requested from the human.

## Automated verification

Review verification on `d0d17aa`: 77 relevant tests passed, including real SIGINT handling, bounded cleanup with an inherited pipe, partial outcomes, metadata structure, isolation, and failed status/audit persistence. Final pre-PR verification on the same application code: all 546 pytest tests and all 41 wrapper checks passed.

## Evidence limits and follow-up

- The live sync contained one mapping. Multi-mapping partial acknowledgement, unknown outcomes, missing/read-failing sources, launch failure, timeout, interrupted writes, and combined audit/status storage failures were tested with synthetic/fake-provider scenarios, not deliberately triggered against GitHub.
- No live audit-content inspection, subprocess environment tracing, or provider network instrumentation was performed. Status and safe command output support the live results; automated tests supply the fault-path and isolation evidence.
- Host package metadata establishes the installed `gh` package version, not an independent attestation of the exact binary executed by the broker. The provider binary was not upgraded, and successful live verification establishes compatibility with the active listing output.
- Human secret submission was through local sudo only. Human entry over SSH (direct or remote sudo), tmux, and SSH into the client remain untested. Agent SSH metadata/execution access does not establish human SSH entry compatibility.
- The local terminal session was previously checked with synthetic input and reported `VTE_VERSION=6800`; the emulator's exact name/version remains unconfirmed. See [interactive entry validation](interactive-secret-entry.md).
- Temporary upgrade helpers/client configuration were used. A clean installation using repository-only instructions, credential renewal, and operational recovery remain separate work.
- The new synthetic vault and GitHub test entries remain available for human cleanup. The older isolation-test entry was not changed.

The complete local-sudo human-entry → vault → separately approved GitHub sync → metadata verification scenario passed. This record does not claim live coverage of every failure path or every supported access configuration.

No credential values, value hashes, private addresses, vault project identifiers, or raw provider output are included in this record.
