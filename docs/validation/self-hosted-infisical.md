# Self-hosted Infisical: Ubuntu with containerd, and a Synology NAS

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
| `sudo nerdctl compose up -d` | All four containers run; about 75 seconds with image downloads. Infisical runs as uid 61001, Redis as 61002 |
| Keys out of sight | No key or password in nerdctl's output, `nerdctl inspect` or the process arguments. As the agent account: Infisical's `/proc/PID/environ` denied; Redis answers `NOAUTH Authentication required.` |
| TLS by IP address | `curl --cacert infisical-root.crt https://192.168.122.164:8443/api/status` answers 200 |
| First admin | Created with `infisical bootstrap`; project (`dev`, `staging`, `prod`), machine identity with Token Auth, project membership and token through the API |
| Broker configuration | `secretops-configure --ca-file`, then `secretops-configure`: `OK vault connection works` |
| First check: vault only | `secretops add FIRST_CHECK_KEY --env dev`: `Saved and verified.` The agent account's `vault.read.check`: `OK secret exists: FIRST_CHECK_KEY` |
| Backup | `backup.sh` with cron's environment (`PATH=/usr/bin:/bin`, run from `/root`): a 183 KB dump, mode 600, no partial file |
| Restore | `down -v` deleted all volumes; database restored into a new instance with the same `secrets/`; Infisical started; after the new Caddy root was set with `--ca-file`, the broker's token still worked and a new check found `FIRST_CHECK_KEY` |
| Docker Compose | Docker Compose v5.8.0 `config` accepts the same file and shows no key; not started with Docker |

## Found

- **nerdctl prints secrets passed as environment.** `nerdctl compose up` logs each `nerdctl run` command line, including every `-e=` value from an `env_file`, and those values are in the process arguments while the command runs. The first version of the compose file used `env_file` as Infisical's example does, and printed `ENCRYPTION_KEY`, `AUTH_SECRET` and the database password. Fixed in the guide's files: the keys are read from root-only files inside the containers (`setpriv` drops to the image's user; Postgres uses `POSTGRES_PASSWORD_FILE`).
- **The image's uid 1001 is a host account's uid.** Without user namespaces a container uid is the same uid on the host, and on this VM uid 1001 was the agent account, which could read Infisical's keys through `/proc/PID/environ` (found by the review). Infisical and Redis now run as 61001 and 61002, `make-env.sh` refuses if a host account has either, and Infisical gets a passwd entry for its uid inside the container (Node needs one).
- **Redis had no password and was reachable from the host.** Container addresses on the bridge network are reachable from host accounts; the agent account got `PONG` (found by the review). Redis now reads `requirepass` from a root-only file.
- **Caddy needs `default_sni` for IP addresses.** A client that connects by IP address sends no server name, and Caddy then fails the TLS handshake. Fixed in the `Caddyfile`.
- **A new Caddy volume changes the CA** (a new machine or `down -v`). Caddy creates a new local CA, and the broker reports `FAIL vault not reachable at the configured URL (connection, DNS or TLS failed)` until the new root is copied and set with `--ca-file`. Documented in the guide. This is the first TLS failure seen against a real vault; the classification was correct.
- **`secretops-configure --ca-file` on a new broker stores only the CA setting** and checks a configuration that has no project yet (`SECRETOPS_INFISICAL_PROJECT_ID is not set`). The guide runs it first and `secretops-configure` second. Open in TODO.
- **The TLS failure message suggests the system CA store** even when `--ca-file` is set. Open in TODO.
- **A missing folder is reported as not found with Infisical CLI 0.45.14** too (`FAIL project, environment or folder not found ...`), as with 0.44.2. The "may look like an empty folder" note after a working check is not needed for these versions (TODO 4j).
- **`secretops add` works with Infisical CLI 0.45.14**, so the adapter's `secrets set` still works with that version.
- **`infisical bootstrap` exits 0 when it fails** (here: the CA was not readable). Check its JSON output, not the exit status.
- **The Infisical CLI's Cloudsmith repository is gone** (since 2026-09-16); a CLI installed from it cannot be upgraded.

## Synology NAS: fresh install beside an old instance

Date: 2026-11-22. A Synology NAS with DSM 7.3.2 (x86_64, 20 GB), Container Manager with Docker 24.0.2 and Compose v2.20.1. An older test Infisical (v0.161.10, Compose project `infisical`, port 8086) kept running. The coding agent did the NAS steps over a temporary SSH key limited to the laptop's address, with a temporary passwordless sudo rule, both removed afterwards. The human made the browser steps, pasted the broker token and ran `secretops add`; the laptop broker was secretops 0.1.14 with Infisical CLI 0.44.2.

| Step | Result |
| --- | --- |
| `sudo sh make-env.sh 192.168.0.94 8443` in `/volume1/docker/infisical-secretops` (root's, mode 755) | `.env` and `secrets/`; `secrets/` in Linux mode (no inherited Synology ACL), not readable by other accounts |
| `sudo docker compose up -d` | Four containers; Infisical uid 61001, Redis 61002, Postgres 70; no key or password in the output |
| TLS | The laptop verifies `https://192.168.0.94:8443` with the copied Caddy root |
| First admin | Created through the bootstrap API; the human finished the setup page (invite-only, email login only) after accepting the browser's certificate warning; the CA was not imported into the browser |
| Project, machine identity, token | Through the API; the token was stored root-only on the NAS, shown once in the human's terminal and deleted |
| Laptop broker | `secretops-configure --ca-file`, then `secretops-configure` with the new URL, project and token: `OK vault connection works` |
| First check: vault only | `secretops add FIRST_CHECK_KEY --env dev`; the agent account's `vault.read.check`: `OK secret exists: FIRST_CHECK_KEY` |
| Backup | `backup.sh` with a scheduled task's environment: a 181 KB dump, mode 600 |
| Restore | `down -v`, restore, new Caddy root: Infisical started and the project was there |

Found on the NAS:

- **DSM has no `getent`**, so `make-env.sh` skipped its uid check without a word. It now reads `/etc/passwd` when `getent` is missing.
- **The fixed Compose project name `infisical` would have taken over the old instance's project.** The compose file no longer sets a name; the directory name is the project name.
- **A non-interactive SSH command on DSM does not find `docker`** (`/usr/local/bin` is not on the path); an interactive login does. Noted in the guide.
- **Switching a configured broker to a new vault with a private CA** takes `--ca-file` first (its check still uses the old vault) and then `secretops-configure`; the first check's result does not matter there.

## Keys export and import

Date: 2026-11-22, in a fresh Ubuntu 22.04 VM with nerdctl 2.4.2, the same images.

| Step | Result |
| --- | --- |
| `sudo COMPOSE="nerdctl compose" sh export-keys.sh` | One archive in the user's home, mode 600, owned by the user: `.env`, `secrets/`, Caddy's root certificate and key; no temporary file left; a directory others can write to is refused |
| A new machine | `down -v`, `.env` and `secrets/` deleted |
| `import-keys.sh ARCHIVE` | `.env` and `secrets/` back (root, 600/700) and the root CA put into the new `caddy_data` volume before the first start; Caddy made a new intermediate from it; a second run refuses; a broken archive changes nothing |
| Restore from `backup.sh` | The project is there; the old `root.crt` still verifies the vault and the restored root certificate is byte for byte the old one, so the broker needs no new `--ca-file` |
| Browser trust | The guide's `certutil` lines create an NSS database when there is none and import the root as `C,,`; a browser itself was not run |

Found: `nerdctl compose run` always asks for a terminal and has no `-T`, so `import-keys.sh` uses `compose create` and `compose cp` into the created container instead. Only the root is exported: Caddy's intermediate lasts 7 days and would be expired in a later restore (found by the review). The restore with a regenerated intermediate was run the same day; one weeks later was not.

## Not tried

- Docker Engine on Ubuntu at runtime, Kubernetes (Helm).
- The run on the owner's work machine.
- A browser that trusts the local CA (the warning was accepted instead).
- Adopting an existing instance: the old NAS instance was a test and is replaced, not adopted.

## One command for admin, project, identity and token

Date: 2026-11-22. A fresh agent preparing an installation (TODO 4k) found the Infisical web page steps (first admin, project, machine identity, Token Auth, token) to be where a new user gets stuck. `examples/infisical/bootstrap-broker.py` does them through the API, run by the human on the broker. Tried in a fresh Ubuntu 22.04 VM with nerdctl 2.4.2, Infisical v0.167.14, Infisical CLI 0.45.14 and secretops 0.1.14 from the release bundle, through `expect`, with made-up values.

| Step | Result |
| --- | --- |
| New vault, configured broker without a token | Asks email and password (hidden, twice); creates admin, organization, project (`dev`, `prod`, `staging`) with the admin as project admin, `secretops-broker` with Token Auth and project membership, a 30-day token; saves URL, project, CA file and token in `broker.env` (600, broker user); deletes the bootstrap identity; `OK vault connection works` |
| Afterwards, in the database | The admin user is the project's admin; `secretops-broker` is the only machine identity left |
| Output | Neither the password nor a token appears |
| Run again | Refuses: the broker already has a project and token |
| Broker without a token, vault with an admin | Refuses before asking anything; nothing changed |

Found (review): a project made by an identity has only that identity as member, so the admin is added as project admin before the bootstrap identity is deleted. Not tried: a broker on another host than the vault (the NAS case), a failure half-way, logging in to the web page as the new admin.
