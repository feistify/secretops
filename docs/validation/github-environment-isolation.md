# GitHub environment isolation validation

Date: 2026-10-18
Tested application and broker wrapper: commit `42cdd70`
Wheel SHA-256: `c5e87a7f52e262145b5998c08df10401bdf67100dfdb18eb16173d4ea205b835`

## Setup

A human installed the built application and matching `secretops-broker` wrapper on the dedicated local broker. The update preserved credentials, stored GitHub login, plans, audit, runtime, provider binaries, policy, and SSH configuration. The existing GitHub CLI was `2.4.0+dfsg1` (Ubuntu); it was not upgraded for this test. The agent used only the restricted SSH broker interface for provider operations.

## Results

| Check | Result |
| --- | --- |
| Existing stored login with isolated child environment | A fresh `github.env_secret.verify_many` plan passed preflight and execution against an existing staging test name |
| GitHub secret write | A saved `github.env_secret.write` plan passed preflight; the human approved the displayed digest; execution reported the dedicated staging test secret updated and the plan ended `executed` |
| Post-write metadata check | A separate `verify_many` plan passed preflight and execution and confirmed the test name was present |

The write used an existing synthetic vault secret from `dev` and target name `NASCHECK_GH_ISOLATION_42CDD70` in a GitHub `staging` environment. The agent never received the value, approved its own plan, or called provider CLIs directly. The test secret remains available for human cleanup.

## Evidence and limits

- Automated review verification: all 380 pytest tests passed for `fe82185`; the subsequent `42cdd70` changed the shell wrapper, wrapper tests, and documentation, with no Python changes. All 37 wrapper checks passed for `42cdd70`.
- Automated tests inspect the exact subprocess environment for writes and metadata checks, exclude unrelated credentials and all GitHub token variables, preserve configured login locations, and exercise trusted executable resolution. Wrapper tests cover inherited `SECRETOPS_GH_BIN` removal and the trusted configuration override.
- The live test confirms stored-login and operation compatibility. It does not independently instrument the actual `gh` process environment or inspect credential files.
- A successful GitHub write is based on the CLI exit status. Metadata presence does not prove byte-exact value equality; no GitHub secret value was read back.
- Keyring-based login, enterprise hosts, custom TLS/proxy settings, and token-only migration were not independently exercised on the real broker.
- Timeouts, unknown write outcomes, and improved partial-sync reporting remain separate work. The complete human-entry workflow is still pending because interactive entry is not implemented.

No credential values, private addresses, or vault project identifiers are included in this record.
