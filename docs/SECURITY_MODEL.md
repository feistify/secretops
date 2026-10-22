# Security model

This document explains how secretops keeps secret values away from agents, which guarantees are enforced, and which ones depend on convention. It ties together the safety invariants below, the agent rules in `docs/AGENT_USAGE.md`, and the broker deployment in `docs/BROKER_HOST.md`.

## What secretops protects

The asset is the **secret value**: passwords, API keys, and other credentials stored in a vault and distributed to targets such as GitHub environment secrets.

The main concern is an **AI agent** that prepares and runs secret operations. The agent should be able to work with secret names, environments, recipes, plans, and statuses, and it should never see a secret value. That holds even if the agent makes a mistake, is careless, or is manipulated through prompt injection.

secretops is not a vault and does not protect the vault itself. It also does not protect secrets that already sit on the agent's own host, for example a `.env` file in the agent workspace.

## Three layers of protection

Protection comes in three layers. Layers 1 and 2 are always present. Layer 3 depends on how secretops is deployed, and only layer 3 turns the agent boundary into an enforced one.

### Layer 1: secretops never reveals values

secretops itself is designed so that values never reach a place the agent can read:

- stdout, stderr, error messages, plan artifacts, and audit logs contain secret names only, never values
- values move between vault and target only during an approved `execute-plan`
- GitHub target writes receive the value on stdin; Infisical writes use a temporary YAML file with `0600` permissions, passed with `--file` and deleted right after use
- values are never passed as command-line arguments, where they would be visible in process listings and shell history
- provider CLI output is captured and suppressed, not echoed
- the Infisical CLI runs with an explicit environment (machine token, fixed `PATH`, empty temporary `HOME`), so broker variables, user sessions, and working-directory config cannot change what secretops reads
- vault answers are checked against a strict response protocol; an error, a malformed answer, or a timeout stops the operation and is never treated as "secret missing"
- an Infisical vault write counts as done only when a read-back returns the written value; otherwise the outcome is reported as unknown, recorded as `outcome_unknown` in the audit log with the partial result for multi-secret operations, and never retried automatically. GitHub target writes are not read back and still rely on the `gh` exit status
- there is no reveal command; `secret.reveal`, `secret.print_value`, and `secret.export_all` are explicitly denied in the default policy
- tests assert that dummy values never appear in CLI output, audit logs, plan artifacts, or command-line arguments

**Limit:** this layer stops secretops from leaking a value by accident. It does not stop an agent that bypasses secretops and calls the vault directly.

### Layer 2: operating rules for agents (convention)

`AGENTS.md` and `docs/AGENT_USAGE.md` tell agents which commands they must not run:

- `secretops approve-plan` and `secretops reject-plan`
- provider CLIs for secrets, such as `infisical secrets` and `gh secret`
- `env` and `printenv` to inspect secrets
- anything that reads `.env` files, tokens, private keys, credentials, or machine identity files

The policy file adds hard rules inside secretops: which actions are allowed, which environments each action may touch, which actions need approval, and that production execution is denied by default.

**Limit:** the agent rules are instructions, not enforcement. If the agent runs as the same Unix user that holds vault credentials, for example an `INFISICAL_TOKEN` in its environment or a logged-in `infisical` CLI, the agent can technically read every secret. secretops itself no longer uses a user session, but that does not stop an agent that has one. The policy file is enforced only as long as the agent cannot edit it.

### Layer 3: privilege separation (broker host model)

The enforced boundary is a separate privilege domain. secretops runs as a dedicated broker user, and the agent reaches it only over SSH with a restricted key. Details and setup steps are in `docs/BROKER_HOST.md`.

```text
 Agent domain                         Broker domain (secretops-broker user)
 ------------                         -------------------------------------
 no vault credentials      --SSH-->   broker.env (0600): vault machine identity
 no shell on the broker               plans/, audit/, recipes/ (0700)
 allowlisted subcommands only         policy.yaml
                                      secretops CLI
                                            |
                                            v
                                      vault (Infisical) / targets (GitHub)
```

What this setup enforces:

- vault credentials live only in the broker domain, unreadable by the agent
- the agent's SSH key is bound to a forced command (`scripts/secretops-agent`) with `restrict`: no shell, no file copy, no port forwarding
- the wrapper accepts only `help`, `version`, `actions`, `plan`, `show-plan`, `preflight-plan`, `execute-plan`, and `write-recipe`, each in an exact argument shape; anything else is rejected before secretops runs
- the agent cannot approve or reject plans; approval needs a human key
- the agent cannot edit plan artifacts, the audit log, or the policy, because the broker user owns them
- secret values never cross into the agent domain

The broker can be a separate machine (NAS, VPS, home server) or a second Unix user on the same machine, reached through `ssh secretops-broker@localhost`. The boundary is the Unix user, not the machine. Which guarantees hold where is summarised in [Deployment levels](#deployment-levels).

## Deployment levels

How much secretops protects depends on how you run it. From weakest to strongest:

1. **No broker.** You and the agent run secretops under your own account, with the token in your environment. Good for trying it out alone.
2. **Local broker, agent as you.** `install.sh` with `--agent-user` set to your own account. The token sits with the broker account, but the agent can use your `sudo`, keys and terminal to reach it.
3. **Local broker, separate agent account.** You, the agent and the broker each have their own account on one machine. This is the recommended setup from [INSTALL.md](INSTALL.md).
4. **Broker on another machine.** The agent's machine holds no credentials at all; see [BROKER_HOST.md](BROKER_HOST.md).

| Guarantee | 1. No broker | 2. Agent as you | 3. Separate agent account | 4. Broker elsewhere |
| --- | --- | --- | --- | --- |
| secretops output never contains values | enforced | enforced | enforced | enforced |
| values never passed as command-line arguments | enforced | enforced | enforced | enforced |
| agent cannot read vault credentials | convention | convention | enforced | enforced |
| agent cannot call the vault or provider CLIs with them | convention | convention | enforced | enforced |
| agent cannot approve plans | convention | convention | enforced | enforced |
| agent cannot change plan status, audit log or policy | convention | convention | enforced | enforced |
| production writes denied by policy | while the policy file is protected | while the policy file is protected | enforced | enforced |
| root on the agent's machine cannot read vault credentials | no | no | no | enforced |
| agent cannot read secrets that are already on its own machine | out of scope | out of scope | out of scope | out of scope |

"Convention" means that the agent's instructions forbid it, but nothing stops an agent that ignores them.

**Recommendation:** level 3 for real secrets. Level 4 when the agent's machine is shared, exposed or less trusted than the secrets. Levels 1 and 2 are a workflow gate: you still get plans, approvals, an audit trail and no values in output, but not a boundary. If you use them for a low-stakes case, say so instead of implying enforcement that is not there.

## Threats and mitigations

| Threat | Mitigation | Layer |
| --- | --- | --- |
| Agent prints a value while running a secret operation | secretops output, plans, and audit carry names only | 1 |
| Value leaks through process listings or shell history | stdin and `0600` temporary files, never arguments | 1 |
| Agent reads secrets directly with a provider CLI | vault credentials exist only in the broker domain | 3 (2 without broker) |
| Agent approves its own plan | `approve-plan` is not on the agent wrapper allowlist | 3 (2 without broker) |
| Agent marks a plan approved by editing its JSON | plan store is owned by the broker user | 3 (2 without broker) |
| Agent runs an operation against production | policy denies production execution before any provider call | 2 and 3 |
| Agent smuggles extra arguments or paths through SSH | forced-command wrapper validates exact argument shapes and rejects shell metacharacters | 3 |
| Malformed `.env` import echoes file content in an error | parser errors never include file content; policy runs before parse | 1 |
| A broker variable with a secret's name is read as if it came from the vault | explicit provider environment; reserved names rejected | 1 |
| A provider error or timeout is read as "secret missing" and a create-only write proceeds | strict response protocol; errors stop the operation | 1 |
| An interrupted vault write is reported as "not stored" and retried, overwriting a value | read-back confirmation of Infisical writes; unknown outcomes are reported and never retried | 1 |
| A planted `infisical` executable earlier in `PATH` receives the token | CLI taken from `SECRETOPS_INFISICAL_BIN` or fixed system directories only | 1 |
| An approved plan runs against another vault project, path, or server after `broker.env` changes | the plan records its vault destination; preflight and execution compare it with the configuration before any provider call | 1 |
| `GH_HOST` or the `gh` default host redirects a GitHub write | the plan records the GitHub host; `gh` gets `--repo HOST/OWNER/REPO` and no `GH_HOST` or `GH_REPO` | 1 |
| `gh` receives the vault token or other broker credentials, or a planted `gh` earlier in `PATH` runs | `gh` gets an explicit environment (stored-login location, TLS, proxy, locale) and is taken from `SECRETOPS_GH_BIN` or fixed system directories only | 1 |
| A plan's content changes after approval (bug, manual edit) | `approve-plan` records a digest of the operation content; preflight and execution refuse a mismatch | 1 (detects changes only while the digest itself is intact) |
| A malformed or hand-edited plan file is interpreted leniently | strict plan schema: duplicate keys, unknown fields, wrong types, `null`, and non-finite numbers are refused and the file is left untouched | 1 |
| A hung or failing `gh` call is reported as "not updated" and retried, sending a value twice or hiding a partial sync | each call has a timeout; anything after launch except success is an unknown outcome (exit 3), sync stops there and reports every mapping; nothing is retried | 1 |
| Agent supplies a value and approves a `vault.secret.add` plan itself | `approve-plan` and `execute-plan` refuse the action; approval is recorded only by the human endpoints, which are not on the agent wrapper allowlist | 3 (2 without broker) |
| A typed or pasted value leaks through echo, arguments, environment, or client files | hidden input from `/dev/tty`; the value travels once in the JSON body on the endpoint's stdin; plans and audit carry metadata only | 1 |
| A multiline paste is stored as its first line, or its next line is shown at or answers `Save?` | entry is refused unless the terminal confirms bracketed paste before the value prompt; a line break inside a paste rejects the whole value; unread input is discarded before `Save?`, a paste there counts as no, and only the letters of yes/no are echoed | 1 |
| The plan or destination changes between the human's review and submission | submission carries the reviewed digest; the broker re-checks digest, destination, policy, and status under a per-plan lock before any provider call | 1 |
| Another writer creates the same name between the final check and an Infisical write | none: Infisical CLI `secrets set` is an upsert; `create_secret` declares `atomic_create = false` | accepted limit |
| Human approves a plan without reading it | none in secretops; quality of review at `show-plan` time | out of scope |

## Residual risks and limits

- **The agent's own host.** Secrets already sitting in the agent workspace, such as `.env` files or tokens, are readable by the agent. secretops helps move them into the vault (`vault.secret.import_file`) but cannot protect them before that.
- **Human review.** Approval is only as good as the review behind it. Read `show-plan` before `approve-plan`.
- **Broker host compromise.** The broker domain holds the vault machine identity. Scope that identity to the projects and environments the broker actually needs.
- **Values in broker memory.** During execution, a value passes through the secretops process on the broker. This is inherent to moving a value from vault to target.
- **Wrapper parsing.** The forced-command wrapper splits arguments on whitespace and does not support quoting. secretops subcommands, plan ids, and recipe names never need spaces.
- **Approval digest is not a signature.** It detects a change to a plan's content as long as the stored digest survives. Someone who can rewrite both the plan and its digest is stopped only by plan store ownership (layer 3).
- **Concurrent plan commands.** Plan writes are atomic, but two commands on the same plan at once (for example an approval racing an execution) are not serialised: the last write wins. One-time execution is not guaranteed. The exception is `vault.secret.add`: submission and rejection hold a per-plan lock, so a plan is completed at most once. The lock does not stop another plan or provider user from writing the same name.
- **Shared OS account for agent and human.** If the agent runs as the human's own user, it may use the human's SSH agent, keys, terminal, or cached sudo, and so reach the human endpoints. `secretops add` does not claim isolation in that case; hardware-backed keys that require touch, or per-use SSH-agent confirmation, strengthen access but do not prove that a specific plan was reviewed.
- **Terminal reports.** Secret entry trusts the terminal's own report that bracketed paste is on. A terminal or multiplexer that reports the mode but does not mark pastes would defeat the multiline check; confirm the terminals you use.
- **Wheel is not code hiding.** Installing secretops as a wheel keeps the source repository out of the agent workspace, but it is not a cryptographic boundary.

## Plan contract version

Every saved plan records `contract_version`. Any change that alters what an existing approved plan does increments it, including a behaviour change that keeps the JSON structure (for example a different default read option or write mode). Preflight and execution refuse plans with another version, so such a change always requires a new plan and a new approval. A change to the canonical form of the approval digest uses a new digest prefix instead (`sha256-v1:` today).

## Recommended setup

Use a machine identity token for every deployment, including human-only use. Do not copy the broker's identity to a laptop: a human runs vault operations through the broker, or gets a separate identity for independent local access.

1. Start with dummy secret names and non-production environments.
2. For human-only use, run secretops under your own user. No boundary is needed, since you already know the values.
3. For agent-driven use, set up the broker host model from `docs/BROKER_HOST.md`, even if the broker is just a second user on the same machine.
4. Pin `SECRETOPS_PLAN_DIR`, `SECRETOPS_AUDIT_PATH`, and `SECRETOPS_POLICY_PATH` outside any agent-writable path.
5. Keep production denied in policy until a dedicated production gate exists.

## Safety invariants

These invariants are part of the product contract:

- never print secret values
- never write secret values into plan artifacts
- never write secret values into audit logs
- never pass secret values through command-line arguments
- use safe transfer paths:
  - stdin for GitHub target writes
  - temporary files with restrictive permissions for Infisical writes
- run the Infisical CLI with an explicit environment, never the broker's own, so local variables, user sessions, and working-directory config cannot affect answers
- treat a vault error as an error, never as "secret missing"
- run a saved plan only against the destination it records: the vault destination must match the configuration, GitHub calls use the planned host, and an approved plan must match its approval digest
- report an Infisical vault write as successful only after a read-back confirms it; an unconfirmed vault write is reported as an unknown outcome and never retried automatically. GitHub target writes still rely on the `gh` exit status
- planning must not generate secret values
- preflight must not generate or write secret values
- secret names may be printed when needed
- secret values must not be printed in stdout, stderr, audit logs, plan files, docs, test output, or error messages
