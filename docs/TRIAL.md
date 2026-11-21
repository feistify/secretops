# Try secretops in a container

One command starts a container with an installed broker, a human account, an agent account, and stand-in programs in place of the vault and GitHub. You can walk through the whole flow (enter, plan, review, approve, execute, audit) in a few minutes. It needs Docker (23.0 or newer, which builds with BuildKit) or a [compatible program](#without-docker): nothing is installed on your machine, and no vault or GitHub account is used.

**For trying only.** The stand-ins keep what you type in a plain file inside the container. Never enter a real secret. Leaving the container removes it; the image stays until you run `docker image rm secretops-trial`.

```sh
git clone https://github.com/feistify/secretops.git && cd secretops
sh trial/run.sh
```

The first start builds the image and takes a few minutes. You then get a shell as `you`, the human, with a short guide on the screen. Open a second terminal for the agent:

```sh
docker exec -it -u agent -w /home/agent secretops-trial bash
```

You type the agent's commands yourself; no coding agent is installed.

## The flow

| Step | Who | Command |
|---|---|---|
| 1 | you | `secretops add DEMO_API_KEY --env dev`, then any made-up value (hidden) |
| 2 | agent | `secretops-local write-recipe sync.yaml < recipes/sync.yaml` |
| | agent | `secretops-local plan --file sync.yaml --save`, note the plan ID |
| | agent | `secretops-local show-plan --id PLAN_ID` |
| | agent | `secretops-local execute-plan --id PLAN_ID` is refused: not approved yet |
| | agent | `secretops-local approve-plan --id PLAN_ID` is denied: approval is human only |
| 3 | you | `sudo -u secretops-broker /usr/local/bin/secretops-broker show-plan --id PLAN_ID` |
| | you | `sudo -u secretops-broker /usr/local/bin/secretops-broker approve-plan --id PLAN_ID` |
| 4 | agent | `secretops-local execute-plan --id PLAN_ID` |
| 5 | you | `sudo trial-status` shows the names the stand-ins hold, never values |
| | you | `sudo tail /opt/secretops/audit/secretops.jsonl` |

Also in `recipes/` for the agent: `generate.yaml` creates a new secret that the agent never sees (needs approval), and `verify.yaml` checks names at the target (needs no approval). Upload each first, as in step 2: `secretops-local write-recipe generate.yaml < recipes/generate.yaml`. As the agent, try reading `/opt/secretops/broker.env` or `/var/lib/secretops-trial/vault.json`: both are refused.

## What is real and what is not

- **Real:** the broker is installed by the same `scripts/install.sh` as a real installation, with the same accounts, restricted SSH access, policy, plans, approval and audit.
- **Stand-in:** `infisical` and `gh` are small programs from `trial/` that accept only the calls secretops makes. The vault project is `trial-project` with environments `dev`, `staging` and `prod`; the GitHub repository is `trial-owner/trial-repo` with environments `dev` and `staging`.
- **Different from a real installation:** `you` has `sudo` without a password, and the broker's keys are created when the image is built. The stand-in token is public and guards nothing; only file permissions keep the agent account away from the stand-in vault file. Nothing here says how a real vault or GitHub behaves on failure.

`sh trial/check.sh` builds the image and runs the same flow without a person.

## Without Docker

Both scripts take another Docker-compatible program in `DOCKER`, for example on a machine with containerd, `nerdctl` and BuildKit:

```sh
DOCKER=nerdctl sh trial/run.sh            # or DOCKER="sudo nerdctl"
```

Then use the same program wherever this page or the guide in the container says `docker`. Docker and nerdctl 2.4 run as root are checked by `trial/check.sh` every week; rootless nerdctl and other programs are not tested.

On a Kubernetes node, extract the nerdctl full bundle outside `/usr/local` (for example under `/opt`) and link only `nerdctl`, `buildctl` and `buildkitd` into `/usr/local/bin`. The bundle carries its own `containerd.service`; under `/usr/local` it replaces the node's own unit at the next restart. With that layout, `DOCKER="sudo nerdctl" sh trial/check.sh` passed on a containerd-only node, and `secretops add` worked through SSH and `nerdctl exec` from a terminal that qualifies ([terminal requirement](BROKER_HOST.md#human-secret-entry)).

Next: [prepare a vault](PREPARE.md) and [install](INSTALL.md).
