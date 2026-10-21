# Design: named vault destinations

Status: proposed, for review before implementation. Planned after the first real-project use (TODO section 7).

## Problem

A broker serves one Infisical server, project and folder, set by the human in `broker.env`. With one Infisical project per application, the human has to switch the destination with `secretops-configure`, and open plans for the other project then need a new plan.

## Proposal

The human keeps a short list of named destinations on the broker. A recipe or `secretops add` selects one by name. Agents choose only among names the human configured; they still cannot name a project or folder.

**Configuration.** One new managed key in `broker.env`, written only by `secretops-configure`:

```sh
SECRETOPS_INFISICAL_DESTINATIONS='app1=00000000-0000-4000-8000-000000000001:/,app2=00000000-0000-4000-8000-000000000002:/api'
```

- A name is `[a-z][a-z0-9-]{0,31}`; the project ID and folder use the existing validators, which exclude `,` and `:`.
- Same server (`INFISICAL_DOMAIN`) and token for all destinations: one machine identity with access to each project.
- `secretops-configure --vault NAME` adds or changes one destination (asks project ID and folder); `--remove-vault NAME` removes one. The first `--vault` converts the existing single project into a named destination (the human names it) and removes `SECRETOPS_INFISICAL_PROJECT_ID` and `SECRETOPS_INFISICAL_PATH`, so a broker is always in exactly one mode.
- Removing the last destination is refused. Returning to single-project mode is not supported in this version.
- A configuration with both modes set, or a malformed or duplicate list entry, is a configuration error: every vault operation stops before any provider call, with no fallback to either mode. `secretops-configure` never writes such a file and names only the line or key that is wrong.

**Selection.**

- Without the new key, nothing changes: no name is used and a recipe with `vault:` is refused.
- With the new key, every vault recipe must name one: `vault: app1`. There is no default, so adding a second destination later cannot silently redirect an old recipe.
- `secretops add --vault app1 NAME --env dev`; the interactive prompt asks for the name when it is missing. The review shows the name with the full destination before the value is entered.
- `vault.secret.copy_many` and `vault.secret.diff_envs` stay within one destination. GitHub sync reads from the named destination.

**Plans and approval.** A plan stores the name as `vault_name`, beside the existing `vault_destination`. Both are in the approved content. Preflight and execution resolve the destination for that name from the current configuration and compare it with the recorded one, as today. A removed or renamed destination, or a changed project or folder, needs a new plan. Plans saved before the conversion have no name; after conversion they are refused with "replan required". `show-plan` prints the name.

**Format compatibility.** The plan contract stays at version 1; the change is additive and explicit:

- Plans get one new optional field, `vault_name` (string, the name pattern above), allowed only for vault actions. It is part of the approved content and its digest.
- Single-project mode writes no `vault_name`. Existing plans and their approval digests stay valid, byte for byte.
- List mode requires `vault_name` in every vault plan. A plan without it is refused in list mode, and a plan with it is refused in single-project mode; both with "replan required", before any provider call.
- An older secretops version rejects a plan that has `vault_name` (unknown field). Rolling back after creating such plans therefore means planning again; the upgrade notes say so.
- The `secretops add` protocol: `prepare-add` gets an optional `--vault NAME` option, and the review response returns the name with the destination. The client sends `--vault` only when the human gave one; an older broker rejects the unknown option, so the client reports "Not saved" instead of writing to the single project.

**Agent discovery.** The agent learns the names from the human, for example in the consumer project's `AGENTS.md`. A command that lists names is left for later.

## Not in this version

Different servers or tokens per destination, per-destination policy, copying between destinations, a default destination, several brokers on one machine.

## Tests and validation

- Configuration: parsing and refusal of invalid names, duplicates and malformed entries, without echoing values; conversion from one project; add and remove; removing the last destination refused; mixed configuration stops vault operations without fallback.
- Recipes and `secretops add`: name required in list mode, refused in single mode, unknown name refused before any provider call.
- Binding: a changed, removed or renamed destination is refused before any provider call; a plan without `vault_name` in list mode and with it in single-project mode is refused; existing plans and digests are unchanged in single-project mode (existing tests unchanged).
- VM: two test projects, `secretops add` into each, one approved sync from each, and one plan refused after its destination was changed.
