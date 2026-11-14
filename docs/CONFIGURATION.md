# Configuration

Broker configuration for the vault and GitHub. On a local broker, `secretops-configure` writes these settings; see [INSTALL.md](INSTALL.md).

## Vault configuration

Every operation that contacts Infisical needs explicit machine authentication and an explicit location. Saving a plan for a vault action needs the location (`SECRETOPS_INFISICAL_PROJECT_ID`, `SECRETOPS_INFISICAL_PATH`, and optionally `INFISICAL_DOMAIN`) but no token and no provider call. `show-plan` and GitHub-only operations need neither.

| Variable | Required | Meaning |
| --- | --- | --- |
| `INFISICAL_TOKEN` | yes | machine identity or service token; no user session is used |
| `SECRETOPS_INFISICAL_PROJECT_ID` | yes | Infisical project to read and write |
| `SECRETOPS_INFISICAL_PATH` | yes | absolute folder path inside the project, for example `/` |
| `INFISICAL_DOMAIN` | no | Infisical instance, default `https://eu.infisical.com` |
| `SECRETOPS_INFISICAL_BIN` | no | absolute path to the `infisical` executable; otherwise only `/usr/local/bin`, `/usr/bin`, and `/bin` are searched, never the caller's `PATH` |
| `SECRETOPS_PROVIDER_TIMEOUT_SECONDS` | no | timeout for each Infisical and GitHub CLI call, default 60 |

`INFISICAL_DOMAIN` must be the address of your own Infisical instance (its cloud region or your self-hosted server); the installer's required `--infisical-domain` writes it to `broker.env`, and `secretops-configure` asks for it, so the default applies only when it is unset.

Breaking change: secretops no longer uses `infisical login` sessions, `.infisical.json` in the working directory, secret imports, personal overrides, or secret reference expansion. Secret names that the Infisical CLI reserves (such as `PATH` or `HOME`) or that secretops passes to the provider (`INFISICAL_*`, `SECRETOPS_*`, `PYTHON*`, `LD_*`) are rejected. The Infisical CLI cannot store empty or whitespace-only values, so such writes are refused before the CLI runs. Details are in `docs/design/infisical-adapter-hardening.md`.

Breaking change: plans are bound to their destination. A saved plan records `contract_version`, the vault destination (backend, canonical domain, project, path), and for GitHub actions the GitHub host (recipe field `github_host`, default `github.com`). `approve-plan` stores a digest of the operation content. Preflight and execution refuse a plan whose content changed after approval or whose vault destination differs from the current configuration, before any provider call. Plans saved by earlier versions can still be shown, but must be planned and approved again. Details are in `docs/design/plan-destination-binding.md`.

## GitHub configuration

GitHub workflows require `gh` and a stored CLI login for the executing broker user. A human configures that login; agents use only the restricted broker interface. See [GitHub authentication and migration instructions](BROKER_HOST.md#github-authentication-human-only).

**Breaking change:** `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, and `GITHUB_ENTERPRISE_TOKEN` are no longer forwarded, including tokens set in `broker.env`. Token-only installations must migrate to a stored broker-user login. The GitHub child process receives an explicit environment allowlist; unrelated credentials such as `INFISICAL_TOKEN` are excluded.

The executable is resolved through `/usr/local/bin:/usr/bin:/bin`, never the caller's `PATH`. For another location, set `SECRETOPS_GH_BIN` to an absolute executable path in trusted `broker.env`. Upgrade both the application and `/usr/local/bin/secretops-broker` from the same version; the wrapper clears inherited executable overrides. Keep the executable and its directory outside agent/broker-user write access.

## Policy file

`SECRETOPS_POLICY_PATH` names the policy file; without it the packaged default (`src/secretops/policies/default.yaml`) applies. The file is checked strictly before every command:

- top level: only `allowed_actions`, `denied_actions`, `actions`, `approval_valid_hours`, `production_environments`
- each entry under `actions`: only `requires_approval`, `allowed_environments`, `production_allowed`

`requires_approval` and `production_allowed` are `true` or `false` without quotes; `allowed_actions`, `denied_actions` and `allowed_environments` are lists of names. `approval_valid_hours` is a whole number from 1 to 168 (default 24, also when the key is absent): how long an `approve-plan` approval stays executable. `execute-plan` refuses an older approval, an approval time in the future, and a plan approved by a version before approval times were recorded; the plan is then `failed` and needs a new plan and approval. Expiry cannot be turned off. `secretops add` approves and writes in one step, so no expiry applies there.

`production_environments` lists the environment names treated as production, whatever the vault product calls them (default `production` and `prod`; an empty list is refused, since it would turn the production block off). It applies to every environment a plan touches: the vault environment, copy and diff source and target, and the GitHub environment. An action may run in such an environment only when its `production_allowed` is `true` **and** its `allowed_environments` lists the name; with `production_allowed: false` it is refused even if listed (`production execution denied by policy: ACTION (environment NAME is a production environment by policy)`). Other names need only `allowed_environments`. If you add a name, also list it under the metadata checks that should run there.

A key may appear only once in each mapping. Action names under `actions` are not checked. Anything else stops every command:

- an unknown or repeated key, or a value of another type: `FAIL policy file PATH: ...`, naming the key and its location or line, and what to correct
- a missing or unreadable file: `FAIL policy file PATH is missing or not readable by this account`
- invalid YAML: `FAIL policy file PATH is not valid YAML at line N`

**Breaking change (production names):** `prod` is now a production environment by default. A custom policy that lists `prod` in `allowed_environments` of an action with `production_allowed: false` now refuses it; a custom policy without `production_environments` gets the default list. Metadata checks need `prod` listed to run there, as the packaged default does.

**Breaking change (approval expiry):** plans approved before the upgrade have no approval time; `execute-plan` refuses them. Plan and approve them again. Plans still `planned` keep working.

**Breaking change:** unknown keys and wrong value types were accepted before (a quoted `"false"` counted as true, a single name instead of a list was read letter by letter). A custom policy with such a key or value, or a repeated key, now stops every broker command until it is fixed.

## Adapters

- Infisical CLI vault adapter
- GitHub CLI environment secret target adapter

secretops invokes these adapters only through narrow code paths that suppress provider stdout/stderr and avoid exposing values.
