# Prepare the vault

What a vault needs before you install a broker. Installation: [INSTALL.md](INSTALL.md).

secretops supports Infisical (Cloud or self-hosted) today; OpenBao is planned ([ROADMAP](ROADMAP.md)). secretops does not install a vault: for Infisical follow its [self-hosting guide](https://infisical.com/docs/self-hosting/overview). The steps below are manual; an automatic first-project setup is being evaluated.

## What any vault needs

- One project (or mount) and folder for this broker.
- Environments or paths for the stages you use, for example `dev` and `staging`.
- A non-human identity with access only to those, able to read and write secrets.
- An access token with a known lifetime; you renew it by hand ([OPERATIONS.md](OPERATIONS.md#renew-the-vault-token)).
- The vault's CLI installed as root on the broker machine.
- Network access from the broker to the vault without a proxy. A private CA must be in the system store (`update-ca-certificates`).

## Infisical steps

1. Create a project. Its ID is in the address bar.
2. In the organization: **Access Control** → **Machine Identities** → **Create**, with the least organization role. Under **Authentication**, replace Universal Auth with Token Auth and set the access token TTL.
3. In the project: **Access Control** → **Machine Identities** → **Add Machine Identity to Project** → **Assign Existing**, choose the identity and a role that can read and write secrets.
4. On the identity page: **Add Token**. Note when it expires. You enter it with `sudo secretops-configure` ([INSTALL.md](INSTALL.md#configure-the-vault)), never through an agent.

## Web UI over plain HTTP

Browsers disable Infisical's copy buttons over plain HTTP, except on `localhost`. Until the vault has a trusted certificate, open it through `localhost`:

- SSH tunnel (encrypted): `ssh -N -L 8086:127.0.0.1:PORT USER@VAULT_HOST`, then `http://localhost:8086`. Some hosts disable SSH forwarding.
- Local relay (not encrypted): `socat TCP-LISTEN:8086,bind=127.0.0.1,fork,reuseaddr TCP:VAULT_HOST:PORT`.
