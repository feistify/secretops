# Self-hosted Infisical

How to run your own Infisical for secretops on one machine, a server or a NAS, with TLS and a tested backup. The files are in [`examples/infisical/`](../examples/infisical/). Afterwards continue with [PREPARE.md](PREPARE.md) and [INSTALL.md](INSTALL.md).

Tested: Ubuntu 22.04 with containerd and nerdctl 2.4.2, without Docker Engine; Infisical v0.167.14, Infisical CLI 0.45.14, secretops 0.1.13; and a Synology NAS with DSM 7.3.2, Docker 24.0.2 and Compose v2.20.1, with secretops 0.1.14 ([record](validation/self-hosted-infisical.md)). Kubernetes is not yet recorded. Infisical's own guide: [Docker Compose](https://infisical.com/docs/self-hosting/deployment-options/docker-compose).

## What you get

- Infisical, Postgres and Redis, with pinned image versions.
- Caddy in front, with TLS from its own local CA, on port 8443 by default (a Synology NAS uses 443 itself).
- Keys and passwords in `secrets/`, readable by root only. The containers read them from files; they are not in the container configuration or on a command line, so they do not appear in nerdctl's output, `ps` or `inspect`. Redis has a password too.
- Infisical and Redis run as uids 61001 and 61002, Postgres as 70. A process is readable through `/proc` by the host account with the same uid, so no host account may have these uids; `make-env.sh` checks this.

Run the vault on a machine of its own or one you trust: root and anyone with container access there can read the keys.

## Choose the container tool

Every command below uses `$COMPOSE`. Set it once in the shell you work in:

```sh
# Human
COMPOSE="nerdctl compose"      # containerd, where Docker Engine is not allowed
COMPOSE="docker compose"       # Docker, for example a Synology NAS over SSH
```

- **containerd with nerdctl:** install the nerdctl full bundle (containerd, CNI plugins, BuildKit) from the [nerdctl releases](https://github.com/containerd/nerdctl/releases) and check it against the release's `SHA256SUMS`, then `sudo tar Cxzf /usr/local nerdctl-full-VERSION-linux-amd64.tar.gz` and `sudo systemctl enable --now containerd`. On a Kubernetes node, read the note in [TRIAL.md](TRIAL.md) first: the bundle must not replace the node's containerd. Commands run as root (`sudo`); rootless nerdctl is not tested.
- **Docker:** Docker Engine with the Compose plugin.
- **Synology NAS:** install Container Manager in DSM, enable SSH (Control Panel → Terminal & SNMP) and work over SSH as an administrator. DSM uses ports 80, 443, 5000 and 5001; keep 8443 or choose another free port. In a non-interactive SSH command `docker` may not be found, because `/usr/local/bin` is not on the path: use an interactive login or `export PATH=$PATH:/usr/local/bin`.

## Set up

1. Copy `examples/infisical/` to the machine, for example to `/opt/infisical` (on a Synology NAS `/volume1/docker/infisical-secretops`), and work in that directory. Its name is the Compose project name and also names the data volumes: choose one that no other Compose project on the host uses (`sudo $COMPOSE ls` lists them), for example not `infisical` when an older Infisical runs there, and keep it afterwards. Root runs the files in it, so make it root's and writable by nobody else:

   ```sh
   # Human
   sudo chown -R root:root DIR && sudo chmod 755 DIR
   ```

   On a Synology NAS, the `docker` shared folder lets administrators and Container Manager write by default; do not give other users write access to it.
2. Write the configuration. `HOST` is the DNS name or IPv4 address that the broker and your browser use for the vault:

   ```sh
   # Human
   sudo sh make-env.sh HOST            # optional second argument: HTTPS port, default 8443
   ```

   It writes `.env` (host and port only) and `secrets/` (root-only), prints no key, and never overwrites. It stops if a host account has uid 61001, 61002 or 70 (Postgres); change the first two in `compose.yaml`, or use another machine. **Copy `secrets/` to offline storage now**, separately from the database backups: without `ENCRYPTION_KEY` in `secrets/backend.env` no backup can be decrypted.
3. Start it and wait until the API answers:

   ```sh
   # Human
   sudo $COMPOSE up -d
   curl -sk https://HOST:8443/api/status        # {"date":...,"message":"Ok",...}
   ```

   The first start downloads the images and takes a few minutes.
4. Copy the TLS root certificate out of Caddy. It is public; the broker and your browser need it:

   ```sh
   # Human
   sudo $COMPOSE cp caddy:/data/caddy/pki/authorities/local/root.crt ./infisical-root.crt
   sudo chmod 644 infisical-root.crt
   curl -s --cacert infisical-root.crt https://HOST:8443/api/status   # must answer without -k
   ```

5. Create the first admin account at `https://HOST:8443` in your browser. The browser warns about the certificate until you import `infisical-root.crt` into it, or check its fingerprint and accept it. Then continue with [PREPARE.md](PREPARE.md#infisical-steps): project, environments and machine identity.

## Connect the broker

Install the broker as in [INSTALL.md](INSTALL.md) with `--infisical-domain https://HOST:8443`. The Infisical CLI on the broker must come from Infisical's current package repository (`artifacts-cli.infisical.com`, [install](https://infisical.com/docs/cli/install)); the old Cloudsmith repository stopped serving the CLI on 2026-09-16, so a CLI installed from it cannot be upgraded.

Then, on the broker, put the root certificate in a root-owned place and configure:

```sh
# Human, on the broker
sudo install -d -m 755 /etc/secretops-ca
sudo install -m 644 infisical-root.crt /etc/secretops-ca/infisical-root.pem
sudo secretops-configure --ca-file /etc/secretops-ca/infisical-root.pem   # then URL, project, folder, token
```

It ends with `OK vault connection works`. secretops 0.1.13 and older store only the CA setting here; run `sudo secretops-configure` after it. The certificate is trusted for the vault only; `gh` keeps the system store ([Operations](OPERATIONS.md#vault-behind-a-private-ca)).

## Back up

```sh
# Human
sudo mkdir -p /var/backups/infisical
sudo COMPOSE="$COMPOSE" bash /opt/infisical/backup.sh /var/backups/infisical
```

It writes a compressed `pg_dump` (mode 600) and leaves no partial file when it fails. It works from any directory, also from a root cron job, for example `30 2 * * * COMPOSE="nerdctl compose" bash /opt/infisical/backup.sh /var/backups/infisical`. On a Synology NAS use DSM's Task Scheduler (Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script, user root) with `COMPOSE="docker compose" bash /volume1/docker/infisical-secretops/backup.sh DIR`. Keep several and copy them off the machine. A dump holds encrypted data and is useless without `secrets/`; keep the two in different places. Redis holds nothing that needs a backup.

## Restore

On the same or a new machine, with the same `secrets/` and `.env`:

```sh
# Human
sudo $COMPOSE up -d db
sudo $COMPOSE exec -T db pg_isready -h 127.0.0.1 -U infisical -d infisical   # repeat until "accepting connections"
gunzip -c BACKUP.sql.gz | sudo $COMPOSE exec -T db psql -q -v ON_ERROR_STOP=1 -U infisical -d infisical
sudo $COMPOSE up -d
```

Restore only into an empty database: on a running instance, first `sudo $COMPOSE down -v`, which deletes its data.

Caddy creates a new local CA whenever its volume is new (a new machine, or after `down -v`), so the broker then reports `vault not reachable ... TLS failed` until you copy the new `root.crt` (step 4 of [Set up](#set-up)) and run `sudo secretops-configure --ca-file ...` again. The broker's token is stored in the database, so it keeps working.

Test a restore once before you rely on the backups.

## Upgrade

Take a backup, change the image tags in `compose.yaml` to the new pinned versions (read Infisical's release notes first), then `sudo $COMPOSE up -d`. Infisical migrates the database at start. Change the Postgres major version only with a dump and restore.

## Adopt an existing instance

For an Infisical that already runs, for example on a NAS, do not reinstall. In this order:

1. Find its configuration (the `.env` or the container settings) and copy `ENCRYPTION_KEY` and `AUTH_SECRET` to offline storage.
2. Back up the database with `pg_dump` from its Postgres container, as `backup.sh` does.
3. Prove a restore on another machine with this directory's `compose.yaml`: run `make-env.sh` there, then replace `ENCRYPTION_KEY` and `AUTH_SECRET` in `secrets/backend.env` with the old ones, in single quotes if they contain `$`, quotes or spaces. Dump the old database with `pg_dump --no-owner --no-privileges`, then follow [Restore](#restore). A dump from Postgres 14 (Infisical's own example) restores into 16; not the other way round.
4. Put TLS in front of it: Caddy as here, or the NAS's own reverse proxy and certificate, then point the broker at its CA as in [Connect the broker](#connect-the-broker).

