# Prepare the vault

What a vault needs before you install a broker. Installation: [INSTALL.md](INSTALL.md).

secretops supports Infisical (Cloud or self-hosted) today; OpenBao is planned ([ROADMAP](ROADMAP.md)). secretops does not install a vault. The steps below are manual; an automatic first-project setup is being evaluated.

## Infisical Cloud or self-hosted

- **Infisical Cloud** (simplest for a first try): create an account at the address of your region, `https://app.infisical.com` (US) or `https://eu.infisical.com` (EU) ([Infisical: Cloud addresses](https://infisical.com/docs/documentation/platform/identities/universal-auth)).
- **Self-hosted:** follow Infisical's [self-hosting guide](https://infisical.com/docs/self-hosting/overview); the address is your server's URL.

This exact address is the vault URL: the human passes it to `install.sh --infisical-domain`, and `sudo secretops-configure` offers it again as the default for "Vault URL" (press Enter).

You do not need to create a test secret by hand: the first check in [INSTALL.md](INSTALL.md#first-check-vault-only) stores one with `secretops add`.

## What any vault needs

- One project (or mount) and folder for this broker.
- Environments or paths for the stages you use, for example `dev` and `staging`.
- A non-human identity with access only to those, able to read and write secrets.
- An access token with a known lifetime; you renew it by hand ([OPERATIONS.md](OPERATIONS.md#renew-the-vault-token)).
- The vault's CLI installed as root on the broker machine.
- Network access from the broker to the vault without a proxy. A private CA must be in the system store (`update-ca-certificates`).

## Infisical steps

1. Create a project. Its ID is in the address bar. **Folder path:** in the simplest case keep secrets at the project root and enter `/` when `secretops-configure` asks (it is the default; press Enter). Another folder is an absolute path such as `/app`.
2. In the organization: **Access Control** → **Machine Identities** → **Create**, with the least organization role. Under **Authentication**, replace Universal Auth with Token Auth and set the access token TTL ([Token Auth](https://infisical.com/docs/documentation/platform/identities/token-auth)).
3. In the project: **Access Control** → **Machine Identities** → **Add Machine Identity to Project** → **Assign Existing**, choose the identity and a role that can read and write secrets (see below).
4. On the identity page: **Add Token**. Its lifetime is set when it is created; note the expiry date and renew it in time ([OPERATIONS.md](OPERATIONS.md#renew-the-vault-token)). You enter it with `sudo secretops-configure` ([INSTALL.md](INSTALL.md#configure-the-vault)), never through an agent.

**What the identity must be able to do**, in each environment you use and in the configured folder: read secrets (secretops loads the folder's secrets to check that a name exists and to copy a value) and create and update secrets (add, generate, rotate, import, copy). secretops never deletes secrets. Choose a built-in or custom project role that allows this and nothing more; roles are described in Infisical's [role-based access controls](https://infisical.com/docs/documentation/platform/access-controls/role-based-access-controls).

## Environment names

`--env` (for example `secretops add NAME --env dev`) and the recipe field `env` name an Infisical environment by its **slug**; secretops passes the value unchanged to the Infisical CLI's `--env`. Use the slug as shown in your own project, not the display name. Slugs are typically `dev`, `staging` and `prod` ([Infisical: GitHub Actions](https://infisical.com/docs/integrations/cicd/githubactions)).

- The environment must exist in the project, and the machine identity must have access to it.
- The default policy allows writes only in `dev` and `staging`. Names are compared exactly, so `Dev` or `development` is refused. Start with `dev`.
- The default policy calls production `production`, but the typical Infisical production slug is `prod`. In such a project, `execute-plan` and `secretops add` refuse `prod` (`environment denied by policy`), also for the read-only checks the policy allows in production, and `production` does not exist in Infisical. Production writes are refused anyway; use `dev` and `staging` for now.
- In a GitHub sync recipe, `env` is the Infisical environment to read from and `github_environment` is the GitHub environment to write to. If `env` is omitted, it is the same name as `github_environment`. Both names must be allowed by the policy; when they differ (for example GitHub `staging` from Infisical `dev`), set `env` explicitly. See [RECIPES.md](RECIPES.md).

## Web UI over plain HTTP

Browsers disable Infisical's copy buttons over plain HTTP, except on `localhost`. Until the vault has a trusted certificate, open it through `localhost`:

- SSH tunnel (encrypted): `ssh -N -L 8086:127.0.0.1:PORT USER@VAULT_HOST`, then `http://localhost:8086`. Some hosts disable SSH forwarding.
- Local relay (not encrypted): `socat TCP-LISTEN:8086,bind=127.0.0.1,fork,reuseaddr TCP:VAULT_HOST:PORT`.
