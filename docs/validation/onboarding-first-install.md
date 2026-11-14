# First installation by the owner

Date: 2026-11-14
Release: v0.1.7 installer bundle, checked against the SHA-256 in the release notes
System: Ubuntu 22.04, Python 3.12 passed with `--python`, Infisical CLI 0.44.2 from the system package
Vault: an existing self-hosted Infisical
Agent: a coding agent running under a separate account created with `--create-agent-user` (no sudo, no password)

## Setup

The owner removed an earlier hand-made broker, installed the release, and configured the vault with `secretops-configure`. A coding agent was then installed without sudo under the agent account and given the consumer-project instructions. Approval and secret entry happened in a separate terminal under the owner's account. All values were synthetic.

## Results

| Step | Who | Result |
| --- | --- | --- |
| Installer with `--create-agent-user` and `--python` | human | Completed; `PASS: agent reaches the broker through the restricted SSH key` |
| `secretops add` of a test secret | human | Destination shown, hidden input, `Saved and verified.` (create and read-back) |
| Saved-plan `vault.read.check` | agent account | `OK secret exists`; value not printed |
| Create-only `vault.secret.generate` plan, preflight | coding agent | Plan saved, target absent, stopped for approval |
| `show-plan`, `approve-plan` | human | Plan reviewed and approved in the human's own terminal |
| `execute-plan` | coding agent | Exit 0, status `executed`, `OK generated secret stored`; no value shown |

The test secrets were deleted afterwards in the Infisical web UI.

## What was hard

- Ubuntu 22.04 needs a newer Python passed with `--python`.
- An Infisical CLI installed under a user's home is refused; the system package is needed.
- The Infisical repository setup reported an `apt-get update` failure caused by an unrelated package source; the Infisical package was available anyway.
- The vault URL, project ID and folder had to be recovered after the old broker was removed.
- The first operation after configuration failed with only `infisical CLI failed with exit status 1`; a newly created token resolved it.
- Hidden input gave no sign that anything was typed.
- At the approval gate the coding agent did not give the human commands or say where to run them; after approval the human had to tell it so and repeat the plan ID.
- Copy and paste between the two accounts did not work.

These led to the documentation changes for the agent handoff and the agent account, and to planned work on a vault connection check and hidden input feedback ([TODO.md](../../TODO.md)).

## Evidence limits

- An existing vault and an earlier broker, not an empty machine.
- The agent handoff needed extra guidance from the human; a fresh session with only the documented instructions is not yet tested.
- GitHub sync was not exercised.
- Results are as reported by the human; no credential or value was given to any agent.
