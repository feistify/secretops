# A workflow job receives a synced secret

Date: 2026-11-15
Broker: the local installation described in the [v0.1.10 upgrade record](onboarding-v0.1.10.md), same machine and vault, same day
Target: a private test repository on GitHub with a `dev` environment
Agents: one coding agent under the separate agent account ran the secret operations through the broker; a second agent under the human's account guided the test and handled the workflow file and its runs

The procedure is the optional last section of the [demo](../DEMO.md#optional-use-the-secret-in-a-workflow), here with a single-secret `github.env_secret.write` plan instead of `sync_many`. The value was made up.

## Results

| Step | Who | Result |
| --- | --- | --- |
| `gh auth login` as the broker account | human | Logged in through the browser |
| `secretops add` of the test name in `dev` | human | `Saved and verified.` |
| Workflow added to the test repository | guiding agent, after the human agreed to the commit and to the merge | One file of 23 lines on the default branch; `[skip ci]` kept the repository's push workflow from running |
| Workflow run before the sync | guiding agent | Failed at the check step: the secret is missing or empty. The failed run stays in the Actions history |
| `github.env_secret.write` plan, preflight | coding agent | Plan saved; the agent gave the human the review and approval commands |
| `show-plan`, `approve-plan` | human | Destination as intended, no value in the plan, matching digest, 24-hour validity |
| `execute-plan`, then a `verify_many` plan | coding agent, after one word from the human | Reported exit 0, write acknowledged, expected name present; no unknown outcome |
| Workflow run after the sync, same commit | guiding agent | Passed, including the check step; nothing about the value was printed |

Before the second run the guiding agent confirmed that the workflow file on the default branch was still the reviewed one.

## What was hard

- Why the broker logs in to GitHub separately, why from `/`, and what GitHub's identity confirmation in the browser is; [INSTALL.md](../INSTALL.md#log-the-broker-in-to-github-only-for-github-sync) now says so.
- Adding a workflow by a push can start the repository's other workflows.
- The account reports the Free plan and the repository is private, where GitHub documents environment secrets as a paid feature. The environment could be created and the secret reached the job anyway. The reason is not known.
- There is no action that deletes a secret, so cleaning up would mean two web UIs.

## Evidence limits

- One repository, one account, one run.
- The human's steps are as reported by the human. The execution and verification results are the coding agent's report as passed on by the human; the broker's audit log was not inspected. The two workflow runs were read directly from GitHub by the guiding agent.
- A passing job shows that a non-empty value reached it, not that it equals the value in the vault.
- The Free-plan discrepancy listed above means this run says nothing about other accounts on the Free plan.
- Cleanup was skipped on purpose: the test secret was left in the vault and in GitHub, since both are test instances. The workflow file stays in the test repository.
