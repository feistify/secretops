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

Breaking change: secretops no longer uses `infisical login` sessions, `.infisical.json` in the working directory, secret imports, personal overrides, or secret reference expansion. Secret names that the Infisical CLI reserves (such as `PATH` or `HOME`) or that secretops passes to the provider (`INFISICAL_*`, `SECRETOPS_*`, `PYTHON*`, `LD_*`) are rejected. The Infisical CLI cannot store empty or whitespace-only values, so such writes are refused before the CLI runs. Details are in `docs/design/infisical-adapter-hardening.md`.

Breaking change: plans are bound to their destination. A saved plan records `contract_version`, the vault destination (backend, canonical domain, project, path), and for GitHub actions the GitHub host (recipe field `github_host`, default `github.com`). `approve-plan` stores a digest of the operation content. Preflight and execution refuse a plan whose content changed after approval or whose vault destination differs from the current configuration, before any provider call. Plans saved by earlier versions can still be shown, but must be planned and approved again. Details are in `docs/design/plan-destination-binding.md`.

## GitHub configuration

GitHub workflows require `gh` and a stored CLI login for the executing broker user. A human configures that login; agents use only the restricted broker interface. See [GitHub authentication and migration instructions](BROKER_HOST.md#github-authentication-human-only).

**Breaking change:** `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, and `GITHUB_ENTERPRISE_TOKEN` are no longer forwarded, including tokens set in `broker.env`. Token-only installations must migrate to a stored broker-user login. The GitHub child process receives an explicit environment allowlist; unrelated credentials such as `INFISICAL_TOKEN` are excluded.

The executable is resolved through `/usr/local/bin:/usr/bin:/bin`, never the caller's `PATH`. For another location, set `SECRETOPS_GH_BIN` to an absolute executable path in trusted `broker.env`. Upgrade both the application and `/usr/local/bin/secretops-broker` from the same version; the wrapper clears inherited executable overrides. Keep the executable and its directory outside agent/broker-user write access.

## Adapters

- Infisical CLI vault adapter
- GitHub CLI environment secret target adapter

secretops invokes these adapters only through narrow code paths that suppress provider stdout/stderr and avoid exposing values.
