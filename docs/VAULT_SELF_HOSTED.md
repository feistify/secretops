# Self-hosted Infisical

How to run your own Infisical for secretops on one machine, a server or a NAS, with TLS and a tested backup. The files are in [`examples/infisical/`](../examples/infisical/). Afterwards continue with [PREPARE.md](PREPARE.md) and [INSTALL.md](INSTALL.md).

Tested: Ubuntu 22.04 with containerd and nerdctl 2.4.2, without Docker Engine; Infisical v0.167.14, Infisical CLI 0.45.14, secretops 0.1.13; and a Synology NAS with DSM 7.3.2, Docker 24.0.2 and Compose v2.20.1, with secretops 0.1.14 ([record](validation/self-hosted-infisical.md)). On Kubernetes, see [VAULT_KUBERNETES.md](VAULT_KUBERNETES.md). Infisical's own guide: [Docker Compose](https://infisical.com/docs/self-hosting/deployment-options/docker-compose).

## What you get

- Infisical, Postgres and Redis, with pinned image versions.
- Caddy in front, with TLS from its own local CA, on port 8443 by default (a Synology NAS uses 443 itself).
- Keys and passwords in `secrets/`, readable by root only. The containers read them from files; they are not in the container configuration or on a command line, so they do not appear in nerdctl's output, `ps` or `inspect`. Redis has a password too.
- Infisical and Redis run as uids 61001 and 61002, Postgres as 70. A process is readable through `/proc` by the host account with the same uid, so no host account may have these uids; `make-env.sh` checks this.

Run the vault on a machine of its own or one you trust: root and anyone with container access there can read the keys.

## Choose the container tool

Not sure what the host has? [`check-node.sh`](../examples/infisical/check-node.sh) is read-only: it reports the container tools, a Kubernetes cluster (node taints, storage and whether it is node-local, ingress, your rights), the ports, the uids the containers need, the proxy (host and port only, also the one containerd or k3s uses), which download sites answer directly or only through the proxy, and whether the proxy intercepts TLS. Run `sh check-node.sh`, and again with `sudo` for containerd's namespaces; once the vault runs, `sh check-node.sh https://VAULT:PORT` also shows whether the vault needs to be in `NO_PROXY`. Keep or share its output; it holds no secret.

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

   It writes `.env` (host and port only) and `secrets/` (root-only), prints no key, and never overwrites. It stops if a host account has uid 61001, 61002 or 70 (Postgres); change the first two in `compose.yaml`, or use another machine. Its last lines say what to keep offline; see [Keep offline](#keep-offline).
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

5. Install the broker and let one command create the admin, project, machine identity and token: [Connect the broker](#connect-the-broker). Or do it by hand: trust the certificate in your browser ([below](#trust-the-ca-in-your-browser)), create the first admin account at `https://HOST:8443` (invite-only sign-up, email login), then follow [PREPARE.md](PREPARE.md#infisical-steps).
6. Export the keys for your password manager: [Keep offline](#keep-offline).

## How TLS works here

Caddy is its own small certificate authority (CA). At the first start it creates a root certificate, valid for about ten years, and from it a 7-day intermediate and the server certificate for `HOST`, both renewed by Caddy. The root's private key (`root.key`) stays in Caddy's `caddy_data` volume.

The root certificate (`root.crt`) is public. Two places need it: the broker, through `secretops-configure --ca-file` ([Connect the broker](#connect-the-broker)), and your browser ([below](#trust-the-ca-in-your-browser)). Whoever has `root.key` can make certificates that both trust for any name, so treat it like the vault's keys: it is in the export from [Keep offline](#keep-offline) and nowhere else.

When the volume is new (a new machine, or after `down -v`), Caddy creates a new CA, unless you put the old one back with `import-keys.sh` before the first start ([Restore](#restore)). A team can later use the company's own CA or a public certificate for a real DNS name instead; then the broker and browsers need no extra CA.

## Trust the CA in your browser

Check the fingerprint first, against the one on the vault host:

```sh
# Human
openssl x509 -in infisical-root.crt -noout -fingerprint -sha256
```

- **Chrome or Chromium on Linux** use their own certificate store, not the system's (`update-ca-certificates` does not help them). Import with `certutil` from `libnss3-tools`, then restart the browser:

  ```sh
  # Human, as the account that runs the browser
  sudo apt install libnss3-tools
  [ -d ~/.pki/nssdb ] || { mkdir -p ~/.pki/nssdb && certutil -d sql:$HOME/.pki/nssdb -N --empty-password; }
  certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "Infisical local CA" -i infisical-root.crt
  certutil -d sql:$HOME/.pki/nssdb -L | grep "Infisical local CA"     # C,,
  ```

  Chromium from the snap store keeps its store in `~/snap/chromium/current/.pki/nssdb`; use that path there.
- **Firefox:** **Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import**, and tick "Trust this CA to identify websites".
- **macOS and Windows:** browsers use the system store (Keychain Access; the Windows certificate manager); not tested here.

## Connect the broker

Install the broker as in [INSTALL.md](INSTALL.md) with `--infisical-domain https://HOST:8443`. The Infisical CLI on the broker must come from Infisical's current package repository (`artifacts-cli.infisical.com`, [install](https://infisical.com/docs/cli/install)); the old Cloudsmith repository stopped serving the CLI on 2026-09-16, so a CLI installed from it cannot be upgraded.

Then, on the broker, put the root certificate in a root-owned place:

```sh
# Human, on the broker
sudo install -d -m 755 /etc/secretops-ca
sudo install -m 644 infisical-root.crt /etc/secretops-ca/infisical-root.pem
```

**New vault (no admin yet):** copy [`bootstrap-broker.py`](../examples/infisical/bootstrap-broker.py) to the broker and run it with the broker's Python:

```sh
# Human, on the broker
sudo /opt/secretops/venv/bin/python -I bootstrap-broker.py --ca-file /etc/secretops-ca/infisical-root.pem
```

It asks only for the first admin's email and password (hidden), creates the admin, the project `secretops` (`dev`, `staging`, `prod`), the machine identity `secretops-broker` with Token Auth and a 30-day token, saves them in the broker without displaying the token, and ends with `OK vault connection works`. Renew the token with `sudo secretops-configure --token-only`. It refuses a vault that already has an admin and a broker that already has a token; details are in the script.

**Vault with an admin already:** create the project, identity and token in the web page ([PREPARE.md](PREPARE.md#infisical-steps)), then:

```sh
# Human, on the broker
sudo secretops-configure --ca-file /etc/secretops-ca/infisical-root.pem   # then URL, project, folder, token
```

It ends with `OK vault connection works`. secretops 0.1.13 and older store only the CA setting here; run `sudo secretops-configure` after it. The certificate is trusted for the vault only; `gh` keeps the system store ([Operations](OPERATIONS.md#vault-behind-a-private-ca)).

## Keep offline

Keep these in your password manager, as attachments of one entry, not only on the vault host:

| What | Why |
| --- | --- |
| `secrets/backend.env` | `ENCRYPTION_KEY`: without it no database backup can be decrypted; `AUTH_SECRET` |
| `secrets/db_password`, `secrets/redis.conf`, `.env` | to start a restored copy with the same settings |
| Caddy's root CA (`root.crt`, `root.key`) | so a restored vault keeps its CA and nothing needs to trust a new one |
| The admin login (email and password) | to reach the web UI |

`export-keys.sh` puts all files from the table into one archive, readable only by you:

```sh
# Human, in the Infisical directory, after the first start
sudo COMPOSE="$COMPOSE" sh export-keys.sh        # writes ~/infisical-keys-DATE.tar.gz
```

Attach the archive to the password manager entry, then delete it from the disk. On a NAS, keep the default (your home): a shared folder can give read access through ACLs that the file mode does not show. Export again after you change a key or a password. Not needed offline: the broker's access token (create a new one) and any bootstrap token (delete it once the admin login works).

## Back up

```sh
# Human
sudo mkdir -p /var/backups/infisical
sudo COMPOSE="$COMPOSE" bash /opt/infisical/backup.sh /var/backups/infisical
```

It writes a compressed `pg_dump` (mode 600) and leaves no partial file when it fails. It works from any directory, also from a root cron job, for example `30 2 * * * COMPOSE="nerdctl compose" bash /opt/infisical/backup.sh /var/backups/infisical`. On a Synology NAS use DSM's Task Scheduler (Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script, user root) with `COMPOSE="docker compose" bash /volume1/docker/infisical-secretops/backup.sh DIR`. Keep several and copy them off the machine. A dump holds encrypted data and is useless without `secrets/`; keep the two in different places. Redis holds nothing that needs a backup.

## Restore

On a new machine, copy `examples/infisical/` into a directory, make it root's as in [Set up](#set-up), and put the keys back from your export before the first start (on the same host, keep the directory's old name: it names the volumes):

```sh
# Human
sudo COMPOSE="$COMPOSE" sh import-keys.sh infisical-keys-DATE.tar.gz
```

It restores `.env`, `secrets/` and Caddy's root CA (Caddy makes a new intermediate from it), refuses a directory that already has `.env` or `secrets/`, and removes what it restored if it fails. Then restore the database:

```sh
# Human
sudo $COMPOSE up -d db
sudo $COMPOSE exec -T db pg_isready -h 127.0.0.1 -U infisical -d infisical   # repeat until "accepting connections"
gunzip -c BACKUP.sql.gz | sudo $COMPOSE exec -T db psql -q -v ON_ERROR_STOP=1 -U infisical -d infisical
sudo $COMPOSE up -d
```

Restore only into an empty database: on a running instance, first `sudo $COMPOSE down -v`, which deletes its data.

With the CA from the export, the broker and browsers keep working unchanged; the broker's token is stored in the database. Without it, Caddy creates a new CA and the broker reports `vault not reachable ... TLS failed` until you copy the new `root.crt` (step 4 of [Set up](#set-up)) and run `sudo secretops-configure --ca-file ...` again.

Test a restore once before you rely on the backups.

## Upgrade

Take a backup, change the image tags in `compose.yaml` to the new pinned versions (read Infisical's release notes first), then `sudo $COMPOSE up -d`. Infisical migrates the database at start. Change the Postgres major version only with a dump and restore.

## Adopt an existing instance

For an Infisical that already runs, for example on a NAS, do not reinstall. In this order:

1. Find its configuration (the `.env` or the container settings) and copy `ENCRYPTION_KEY` and `AUTH_SECRET` to offline storage.
2. Back up the database with `pg_dump` from its Postgres container, as `backup.sh` does.
3. Prove a restore on another machine with this directory's `compose.yaml`: run `make-env.sh` there, then replace `ENCRYPTION_KEY` and `AUTH_SECRET` in `secrets/backend.env` with the old ones, in single quotes if they contain `$`, quotes or spaces. Dump the old database with `pg_dump --no-owner --no-privileges`, then follow [Restore](#restore). A dump from Postgres 14 (Infisical's own example) restores into 16; not the other way round.
4. Put TLS in front of it: Caddy as here, or the NAS's own reverse proxy and certificate, then point the broker at its CA as in [Connect the broker](#connect-the-broker).

