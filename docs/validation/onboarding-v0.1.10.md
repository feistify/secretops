# Upgrade, fresh-agent handoff and vault failures: v0.1.10

Date: 2026-11-15
Release: v0.1.10 installer bundle, checked against the SHA-256 in the release notes
System: Ubuntu 22.04, Python 3.12, Infisical CLI 0.44.2
Vault: an existing self-hosted Infisical
Agent: a coding agent in a fresh session under a separate account; `sudo -l` lists `use_pty`, so the documented `sudo -iu` start can be used

Follows the [first installation](onboarding-first-install.md). All values were synthetic. The human reports deleting the test secret; the test token was revoked.

## Upgrade

| Step | Who | Result |
| --- | --- | --- |
| `install.sh --upgrade` from v0.1.7 | human | v0.1.10 installed; configuration, policy, plans, audit, SSH service and keys kept. An agent confirmed the installed version from the package metadata |
| Policy: `prod` added to the three metadata checks | human, file prepared by an agent | The new version accepts the file and production writes stay denied, checked with the policy validator without any provider operation. The old file was copied aside first |
| `sudo secretops-configure --check` | human | `OK vault connection works: URL, token, project and environment dev answered.` |
| New token through `secretops-configure --token-only` | human | `Input received` with a character count, no value shown; the check after saving passed. The `secretops add` prompt was not tried here |

## Fresh-agent handoff

The agent's project held only the consumer template from [AGENT_USAGE.md](../AGENT_USAGE.md), with the broker command and `HUMAN_BROKER` filled in. The task was one sentence: generate a named 32-character password in `dev` without overwriting.

| Step | Result |
| --- | --- |
| Plan and preflight | Create-only plan saved, preflight passed |
| Handoff | Without a corrective prompt, the agent gave ready `show-plan` and `approve-plan` commands with the real plan ID, named a separate terminal under the human's account, listed what to review, and waited |
| Approval | The human reviewed and approved; matching digest, 24-hour validity |
| Continuation | After the single word "done" from the human, the agent reported that it executed the plan and checked that the name exists, without asking for the plan ID again |

No corrective prompt was given. Two things in the agent's answer were wrong: the alternative `reject-plan` command was shortened and lacked the human broker command, and it said the value is generated in the vault (the broker generates it and stores it in the vault).

## Vault failures

The human caused each case on purpose. Where the saved configuration was changed, the human restored it afterwards and the check reported a working connection again.

| Case | How | Result of `secretops-configure --check` |
| --- | --- | --- |
| Environment that does not exist | `--check --env NAME` | `FAIL project, environment or folder not found, or not permitted for this machine identity.` |
| Folder that does not exist | saved through `secretops-configure` | the same message |
| Project that does not exist | a made-up project ID | the same message |
| Unreachable URL | a local port that refuses connections | `FAIL vault not reachable at the configured URL (connection, DNS or TLS failed).` |
| Revoked token | a separate test token, revoked after a passing check | `FAIL the vault refused the access token (expired, revoked or wrong).` |

Every message came with its next step, and no provider output or value was shown. After a failing check that followed a save, the output said that the configuration was saved but the check failed. No correction of the classification was needed.

## What was hard

- Copy and paste between the two accounts still does not work (`Authorization required, but no authorization protocol specified`), so the handoff commands were carried by screenshot. The agent account must not get access to the human's graphical session.
- The policy change at upgrade was manual: copy the old file aside, replace it, check it. It is easy to get wrong.
- The steps for an upgrade, the agent setup and these checks are spread over several documents.
- Adding the template to a project that already has an `AGENTS.md` is not described; replacing the file would lose the project's own instructions.
- The working-connection message warns that a folder path that does not exist may look like an empty folder. With this vault and CLI a missing folder was reported as not found, so the warning may not apply.

These are tracked in [TODO.md](../../TODO.md).

## Evidence limits

- One machine, one self-hosted vault, one CLI version, one coding agent.
- Results are as reported by the human, from terminal output and screenshots; the broker's audit log and the agent's own commands were not inspected. No credential or value was given to any agent.
- Only the approval path was exercised: not rejection, an expired approval, or an interrupted session.
- The unreachable case is one observation of a refused connection. DNS and TLS failures and a permission error were not caused. The provider's raw output was not collected, so which pattern matched is not known.
- GitHub sync was not exercised.
