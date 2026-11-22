# Agent-prepared installation: first run

Date: 2026-11-22 (TODO 4k, "Agent-prepared installation")
System: a fresh Ubuntu 22.04 VM (cloud image), 4 CPUs, 6 GB; a human account with sudo; no vault, no Docker
Result: **done criterion reached**: the agent account's `vault.read.check` answered `OK secret exists: FIRST_CHECK_KEY`.

## How it was run

A fresh coding agent got SSH access to the human's account on the VM (no sudo password), the repository URL and one message: "I want to use secretops (https://github.com/feistify/secretops) on this machine with my coding agents. I have no vault yet. Prepare everything for me." The owner played the new user in their own terminal; a second agent relayed messages verbatim between them and did not help, except for SSH access to the VM and, half-way, writing `bootstrap-broker.py` (below).

## What happened

| Time | Step |
| --- | --- |
| 14:06 | Start. The agent checks the machine (Ubuntu 22.04, Python 3.10, free ports, no secretops) and writes a checklist with **Infisical Cloud** |
| | The human answers that the vault must be on-prem, without Docker Engine. The agent finds `VAULT_SELF_HOSTED.md`, copies the four files to the VM and writes a nine-step plan: Python 3.12, nerdctl 2.4.2 (with the correct SHA-256), `make-env.sh`, compose up, CA, web page steps, Infisical CLI, installer, `secretops-configure`, first check, backup |
| ~14:35 | Infisical runs (`/api/status` Ok), CA copied and checked |
| | **Stuck at the web page steps** (first admin, project, machine identity, Token Auth, token). The human asks the agent to do them; it refuses correctly (admin password and token stay with the human), gives click-by-click steps, and notices that the vault already has an admin (the human had made one) |
| ~14:50–16:00 | `examples/infisical/bootstrap-broker.py` written, tested in a second VM, reviewed twice and merged (PR #110) |
| | The human wipes the vault (`down -v`), installs secretops 0.1.14 (`PASS`), runs `bootstrap-broker.py` (one email, one password, `OK vault connection works`), then `secretops add FIRST_CHECK_KEY --env dev` (`Saved and verified.`); the admin sees the project and the key in the web page |
| 16:38 | The agent's check from the `agent` account (`sudo -iu agent`): `OK secret exists: FIRST_CHECK_KEY` |

About 2.5 hours, of which about one hour was writing and reviewing the script; the human ran about ten blocks of commands and chose one password. The agent never ran `sudo`, never saw a token or password, and kept approval with the human. Not reached: the consumer template in a project's `AGENTS.md` (no real project; TODO 4h/4i).

## Found

1. **The agent did not find the self-hosted guide** and offered Infisical Cloud, thinking self-hosting needs Docker. Fixed: `AGENT_START.md` asks about on-prem first and points to `VAULT_SELF_HOSTED.md`.
2. **The vault's web page steps stop a new user.** Fixed by `bootstrap-broker.py` (PR #110); `PREPARE.md` and `AGENT_START.md` point to it.
3. **The checklist did not say where each command runs** (the VM or the human's computer). `AGENT_START.md` now asks for it.
4. **Pasted commands broke** where the chat wrapped them (`curl` without URL, `--infisical-domain` without value). `AGENT_START.md` asks for one line per command. A script for the whole human part would remove the problem; open.
5. **`curl -s` of the 250 MB nerdctl bundle looked stuck.** `AGENT_START.md` asks to say when a download is large.
6. **`PLAN_ID` was typed literally.** `INSTALL.md` now says so next to the command.
7. **Offline keys:** the agent told the human to copy `secrets/` by hand instead of `export-keys.sh`, which also carries Caddy's root key. `AGENT_START.md` names `export-keys.sh`.
8. **The Infisical CLI is not pinned** (apt installs the newest); the adapter was tested with 0.44.2 and 0.45.14. Open.
9. **A CA file in a directory that does not exist** is reported as "has a directory that cannot be read". Open (small message fix).

## Next

Run it again on a clean VM with these fixes, without help: the agent should find `bootstrap-broker.py` itself; measure the time and count the human steps.
