# Design: name-pattern policy rules

Status: draft for review
Depends on: security hardening merged in PR #1 (`eb39f89`)
Prepares for: capability grants / standing approvals (deferred; see [../ROADMAP.md](../ROADMAP.md) and [../../TODO.md](../../TODO.md))

## Goal

Add a third policy dimension — the secret name — so policy can express per-name
autonomy and restriction, for example:

- `BACKUP_*` in dev/staging: agents execute without approval
- `PROD_*`: always requires approval, in every environment
- `STRIPE_LIVE_*`: denied entirely for write-style actions

Today policy is action × environment. This design adds action × environment ×
name pattern. It is the matching engine that grants will later reuse: a grant
is a time-boxed, quota-limited overlay of the same rule shape.

## Non-goals (v1)

- Rules must not widen the action gate. `allowed_environments`,
  `production_allowed`, and the allowed/denied action lists remain hard gates
  evaluated first. A name rule can only deny more or change the approval
  requirement. Opening production per name is a grants-era decision.
- No regex. Glob `*` only.
- No TTL, quota, or per-grant identity. That is the grants feature.

## Policy file schema

New optional top-level `secret_rules` list in the policy YAML:

```yaml
secret_rules:
  - name: deny-live-payment-keys
    match:
      patterns: ["STRIPE_LIVE_*"]
      actions: ["vault.secret.*", "github.env_secret.*"]
    effect: deny

  - name: prod-names-always-require-approval
    match:
      patterns: ["PROD_*"]
    effect: require_approval

  - name: backup-autonomous-in-dev-staging
    match:
      patterns: ["BACKUP_*"]
      environments: ["dev", "staging"]
      actions: ["vault.secret.copy_many", "github.env_secret.sync_many"]
    effect: allow_without_approval
```

Rule fields:

- `name` (required): unique identifier, printed in output and audit events.
  Safe metadata only.
- `match.patterns` (required): non-empty list of name globs. Allowed pattern
  alphabet is `^[A-Z0-9_*]+$`. `*` matches any run of valid name characters.
  Anything else fails policy load. A bare `*` is legal and matches every
  name — it is a global match and should be used with care, especially with
  `allow_without_approval`; the most-restrictive-wins lattice still protects
  any name also matched by a stricter rule.
- `match.environments` (optional): list of environment names. Omitted = all.
- `match.actions` (optional): list of action names; `*` glob allowed with the
  alphabet `^[a-z0-9_.*]+$` (digits included for future action families such
  as `k8s.*` or `oauth2.*`). Omitted = all.
- `effect` (required): one of `deny`, `require_approval`,
  `allow_without_approval`. Any other value fails policy load.

Policy load is fail-closed: a malformed rule (missing fields, bad pattern,
unknown effect, duplicate rule name) raises an error. Rules are never silently
skipped.

An absent or empty `secret_rules` section means no behavior change at all —
existing policies and tests remain valid.

## Evaluation semantics

### Per-name decision

For one `(action, environment, secret_name)` tuple, collect every rule whose
patterns, environments, and actions all match. The decision is the most
restrictive effect among matching rules:

```text
deny  >  require_approval  >  (no rule: action-level default)  >  allow_without_approval
```

Rule order in the file is irrelevant. Evaluation is order-independent so that
policy review does not depend on list position and so grants can concatenate
rules later without re-sorting.

### Per-plan aggregation

A plan may involve several names and environments. The plan decision aggregates
all its tuples:

1. If any tuple is `deny` → the plan is denied.
2. Else if any tuple is `require_approval` → the plan requires approved status.
3. Else if at least one tuple has no matching rule → the action-level
   `requires_approval` applies unchanged.
4. Else (every tuple matched `allow_without_approval` and nothing stricter) →
   the approval requirement is waived; the plan may execute from `planned`
   status.

The waiver only affects the approval requirement. It never bypasses the action
allow/deny lists, environment lists, or `production_allowed`.

### Which tuples a plan produces

| Action | Tuples |
| --- | --- |
| `vault.read.check` | `(action, env, secret_name)` |
| `vault.secret.generate` / `rotate` / `import_file` | `(action, env, secret_name)` |
| `vault.secret.copy_many` | each `(action, source_env, source)` and `(action, target_env, target)` |
| `vault.secret.diff_envs` | each `(action, source_env, name)` and `(action, target_env, name)` |
| `github.env_secret.write` | `(action, env, secret_name)` and `(action, target_environment, target_secret_name)` |
| `github.env_secret.sync_many` | each `(action, env, source)` and `(action, target_environment, target)` |
| `github.env_secret.verify_many` | each `(action, target_environment, name)` |

Metadata-only actions (`diff_envs`, `verify_many`, `read.check`) are still
subject to `deny` rules. If a policy author wants to deny writes but allow
existence checks, they scope the deny rule with `match.actions`.

## Enforcement points

1. **Recipe planning (`plan` command).** Evaluate before saving the plan
   artifact. A `deny` refuses to create the plan and prints the rule name.
   `require_approval` / waiver outcomes are printed so the agent knows up
   front whether approval will be needed, but the decision is **not persisted
   in the plan artifact**. The artifact stays a description of intent;
   execution re-evaluates from scratch, so there is no stored decision to
   trust or tamper with.
2. **Execution (`execute-plan`).** The authoritative gate. Extend
   `_require_plan_execution_allowed` in `plan_lifecycle.py`: after the existing
   action/environment/production checks, evaluate the plan tuples.
   - `deny` → fail before any provider call, plan status `failed`, audit
     failure event naming the rule.
   - `require_approval` (from rules or action default, unless waived) → the
     existing approved-status check applies.
   - waiver → skip the approval requirement; write an audit event recording
     that approval was waived and by which rule (see Audit).
   Saved plans are untrusted input; execution never trusts a decision recorded
   at planning time and always re-evaluates.
3. **Preflight.** Report the evaluation outcome (deny / approval required /
   waived, with rule names) without mutating plan status, consistent with
   preflight being side-effect free.

## Code changes

- `src/secretops/policy.py`
  - `SecretRule` frozen dataclass: `name`, `patterns`, `environments | None`,
    `actions | None`, `effect`, plus an internal `source: str = "base_policy"`
    field. `source` is not parsed from YAML in v1; grants will construct rules
    with `source="grant:<grant_id>"` so audit events and the severity
    refinement below can tell rule origins apart without reshaping the model.
  - `Policy` gains `secret_rules: list[SecretRule]`.
  - Loader parses and validates the section (fail-closed).
  - `evaluate_secret_rules(policy, action, env, name) -> RuleDecision` and
    `evaluate_plan_rules(policy, tuples) -> PlanRuleDecision` implementing the
    lattice above. Pattern matching via translation of `*` to `[A-Z0-9_]*`
    anchored regex (do not use `fnmatch` — its `*` crosses any character;
    equivalent here but the explicit translation keeps the alphabet
    guarantee visible and testable).
- `src/secretops/plan_lifecycle.py`
  - A per-action tuple extractor (table-driven, mirroring the table above).
  - Wire evaluation into `_require_plan_execution_allowed` and preflight
    output.
- `src/secretops/handlers.py`
  - Wire evaluation into recipe planning; print outcome lines.
- `src/secretops/policies/default.yaml`
  - Ship with `secret_rules: []` plus commented examples, so the mechanism is
    visible but changes nothing by default.

## Audit

- Denials: existing failure audit event, extended with the deciding rule name.
- Waivers: **one** audit event per plan (not per name), written at execution
  time —
  `{"action": ..., "secret_name": "multiple" | "<name>", "env": ...,
  "plan_id": ..., "status": "approval_waived", "rules": ["<rule-name>", ...],
  "matched_secret_count": N}`. Multi-name plans keep the existing
  `secret_name: "multiple"` audit convention; the individual names are
  recoverable via `plan_id`. `rules` lists every waiver rule that
  participated. An execution without human approval is a security-relevant
  event and must be reconstructible from the audit log. This is also the
  audit shape grants will need (`approval_waived` by grant id).
- Rule names and patterns are safe metadata. Secret values never appear in
  rules, decisions, or audit events (unchanged invariant).

## CLI addition (small, recommended)

`secretops explain-policy --action A --env E --name N` prints the matching
rules and the resulting decision. Cheap to build on top of
`evaluate_secret_rules`, and it gives both humans and agents a way to
understand a denial without reading YAML. Names only; no provider calls.

## Forward compatibility with grants

Design the evaluation entry point as `evaluate(rules, tuples)` where `rules`
is a list, not a `Policy` attribute lookup. Grants will concatenate their own
`SecretRule`-shaped entries (tagged with a source) onto the base list. Planned
severity refinement at that point: a grant-sourced waiver outranks a
base-policy `require_approval` (the grant *is* the pre-approval), but nothing
from any source ever outranks `deny`. Keep the severity comparison in one
function so this refinement lands as a small change.

## Tests

- Pattern validation: reject lowercase, spaces, regex metacharacters, empty
  patterns; accept `BACKUP_*`, `*_KEY`, bare `*`.
- Precedence: deny beats waiver on the same name; `require_approval` beats
  waiver; mixed multi-name plans aggregate to the strictest member; unmatched
  name falls back to action default.
- Hard gates unchanged: a waiver rule cannot execute an action in a forbidden
  environment or production; denied actions stay denied.
- Backward compatibility: policy without `secret_rules` produces byte-identical
  behavior; full existing suite passes untouched.
- Fail-closed load: malformed rule fails `load_policy`, including via
  `SECRETOPS_POLICY_PATH` override.
- End-to-end with fake providers: denied plan → no provider call, status
  `failed`, audit names the rule; waived plan executes from `planned` status
  and writes the `approval_waived` audit event; re-evaluation happens at
  execute time even if the saved plan JSON was edited after planning.
- No-leak: rule processing never prints secret values (extend the existing
  leak assertions).
