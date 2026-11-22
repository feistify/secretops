# Self-hosted Infisical with containerd: first run

Date: 2026-11-22
Guide: [VAULT_SELF_HOSTED.md](../VAULT_SELF_HOSTED.md), files in `examples/infisical/`
System: a fresh Ubuntu 22.04.5 VM (cloud image), containerd and nerdctl 2.4.2 from the nerdctl full bundle, no Docker Engine
Images: `infisical/infisical:v0.167.14`, `postgres:16.16-alpine`, `redis:7.4.12-alpine`, `caddy:2.11.9-alpine`
Broker: secretops 0.1.13 from the release bundle (SHA-256 checked against the release notes), `--python /usr/bin/python3.12` (deadsnakes), Infisical CLI 0.45.14 from `artifacts-cli.infisical.com`, a separate agent account

A coding agent ran every step, including the human ones, in a throwaway VM with made-up keys, passwords and values, at the owner's request. The human steps were driven through `expect`; `secretops add` refuses a terminal that does not confirm bracketed paste, so `expect` answered that query as a terminal would. The VM was deleted afterwards.

## Results

| Step | Result |
| --- | --- |
| `sudo sh make-env.sh 192.168.122.164` | `.env` with host and port; `secrets/` with `backend.env` and `db_password`, mode 600, owner root |
| `sudo nerdctl compose up -d` | All four containers run; about 75 seconds with image downloads. Infisical runs as uid 1001 |
| Keys out of sight | No key or password in nerdctl's output, in `nerdctl inspect`, or in the process arguments |
| TLS by IP address | `curl --cacert infisical-root.crt https://192.168.122.164:8443/api/status` answers 200 |
| First admin | Created with `infisical bootstrap`; project (`dev`, `staging`, `prod`), machine identity with Token Auth, project membership and token through the API |
| Broker configuration | `secretops-configure --ca-file`, then `secretops-configure`: `OK vault connection works` |
| First check: vault only | `secretops add FIRST_CHECK_KEY --env dev`: `Saved and verified.` The agent account's `vault.read.check`: `OK secret exists: FIRST_CHECK_KEY` |
| Backup | `backup.sh`: a 188 KB dump, mode 600 |
| Restore | `down -v` deleted all volumes; database restored into a new instance with the same `secrets/`; Infisical started, the broker's token still worked, and a new check found `FIRST_CHECK_KEY` |
| Docker Compose | Docker Compose v5.8.0 `config` accepts the same file and shows no key; not started with Docker |

## Found

- **nerdctl prints secrets passed as environment.** `nerdctl compose up` logs each `nerdctl run` command line, including every `-e=` value from an `env_file`, and those values are in the process arguments while the command runs. The first version of the compose file used `env_file` as Infisical's example does, and printed `ENCRYPTION_KEY`, `AUTH_SECRET` and the database password. Fixed in the guide's files: the keys are read from root-only files inside the containers (`setpriv` drops to the image's user; Postgres uses `POSTGRES_PASSWORD_FILE`).
- **Caddy needs `default_sni` for IP addresses.** A client that connects by IP address sends no server name, and Caddy then fails the TLS handshake. Fixed in the `Caddyfile`.
- **A restore on a new volume changes the CA.** Caddy creates a new local CA, and the broker reports `FAIL vault not reachable at the configured URL (connection, DNS or TLS failed)` until the new root is copied and set with `--ca-file`. Documented in the guide. This is the first TLS failure seen against a real vault; the classification was correct.
- **`secretops-configure --ca-file` on a new broker stores only the CA setting** and checks a configuration that has no project yet (`SECRETOPS_INFISICAL_PROJECT_ID is not set`). The guide runs it first and `secretops-configure` second. Open in TODO.
- **The TLS failure message suggests the system CA store** even when `--ca-file` is set. Open in TODO.
- **A missing folder is reported as not found with Infisical CLI 0.45.14** too (`FAIL project, environment or folder not found ...`), as with 0.44.2. The "may look like an empty folder" note after a working check is not needed for these versions (TODO 4j).
- **`secretops add` works with Infisical CLI 0.45.14**, so the adapter's `secrets set` still works with that version.
- **`infisical bootstrap` exits 0 when it fails** (here: the CA was not readable). Check its JSON output, not the exit status.
- **The Infisical CLI's Cloudsmith repository is gone** (since 2026-09-16); a CLI installed from it cannot be upgraded.

## Not tried

- Docker Engine at runtime, a Synology NAS, Kubernetes (Helm).
- A browser login with the local CA.
- The run on the owner's work machine.

## Adopting an existing instance

Template for the owner's run on the NAS; one line per step, no values.

| Step | Result |
| --- | --- |
| Keys found and copied offline | |
| Database dump | |
| Restore proved on another machine | |
| TLS in front, broker `--ca-file`, `secretops-configure --check` | |
| What was hard | |
