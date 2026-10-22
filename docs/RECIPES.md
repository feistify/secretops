# Recipe reference

A recipe is a small YAML file that says what should happen: which action, which secret names, which environments, which GitHub repository. It never contains a value. An agent uploads it and saves a plan from it:

```sh
secretops-local write-recipe task.yaml < task.yaml
secretops-local plan --file task.yaml --save
```

Every example on this page is checked by the tests: it must load and produce a saved plan.

## Rules for all recipes

- **Secret names:** capital letters, digits and `_`, starting with a letter, for example `STRIPE_KEY`. Names starting with `SECRETOPS_`, `INFISICAL_`, `LC_`, `XDG_`, `LD_` or `PYTHON`, and names such as `HOME`, `PATH` or `USER`, are reserved and refused.
- **Environments:** every environment a recipe names must be allowed by the policy. Under the default policy that is `dev` and `staging` for writes, plus `production` for the three checks that only read names. This includes the GitHub environment: a GitHub environment called `preview` is refused.
- **Vault location:** the Infisical server, project and folder come from the broker configuration, never from the recipe.
- **GitHub repository:** `github_owner`, `github_repo` and `github_environment` name the target. `github_host` is optional and defaults to `github.com`. The environment must already exist in the repository.
- **No values:** a recipe holds names and metadata only. There is no field for a value.

## Actions at a glance

| Action | What it does | Needs approval | Environments (default policy) |
| --- | --- | --- | --- |
| [`vault.read.check`](#vaultreadcheck) | Check that a name exists in the vault | no | dev, staging, production |
| [`vault.secret.add`](#vaultsecretadd) | A human enters a new value | the human confirms in `secretops add` | dev, staging |
| [`vault.secret.generate`](#vaultsecretgenerate) | Generate a new random value | yes | dev, staging |
| [`vault.secret.rotate`](#vaultsecretrotate) | Replace an existing value with a random one | yes | dev, staging |
| [`vault.secret.copy_many`](#vaultsecretcopy_many) | Copy secrets between vault environments | yes | dev, staging |
| [`vault.secret.diff_envs`](#vaultsecretdiff_envs) | Compare which names exist in two environments | no | dev, staging, production |
| [`vault.secret.import_file`](#vaultsecretimport_file) | Import a dotenv file the human placed on the broker | yes | dev, staging |
| [`github.env_secret.write`](#githubenv_secretwrite) | Copy one vault secret to a GitHub environment | yes | dev, staging |
| [`github.env_secret.sync_many`](#githubenv_secretsync_many) | Copy several vault secrets to a GitHub environment | yes | dev, staging |
| [`github.env_secret.verify_many`](#githubenv_secretverify_many) | Check that names exist in a GitHub environment | no | dev, staging, production |

---

## vault.read.check

Checks that a secret name exists in the vault. The value is not read into the output.

| Field | Required | Meaning |
| --- | --- | --- |
| `secret_name` | yes | name to check |
| `env` | yes | vault environment |

```yaml
action: vault.read.check
secret_name: STRIPE_KEY
env: staging
```

## vault.secret.add

Stores one new secret whose value only the human has, for example an API key from a provider console. The agent saves the plan; the human runs `secretops add --plan PLAN_ID`, checks the destination, types the value at a hidden prompt and confirms. Do not run `execute-plan` for this plan.

| Field | Required | Meaning |
| --- | --- | --- |
| `secret_name` | yes | name of the new secret; refused if it already exists |
| `env` | yes | vault environment |
| `create_mode` | no | `create_only` (default and only value) |
| `value_source` | no | `human_prompt` (default and only value) |

```yaml
action: vault.secret.add
secret_name: STRIPE_KEY
env: staging
```

## vault.secret.generate

Generates a random value and stores it as a new secret. Preflight fails if the name already exists.

| Field | Required | Meaning |
| --- | --- | --- |
| `secret_name` | yes | name of the new secret |
| `env` | yes | vault environment |
| `generator.type` | yes | `password` |
| `generator.length` | no | number of characters, default 32; letters, digits, `_`, `-` and `.` |
| `create_mode` | no | `create_only` (default) |

```yaml
action: vault.secret.generate
secret_name: SESSION_SECRET
env: staging
generator:
  type: password
  length: 48
```

## vault.secret.rotate

Replaces the value of an existing secret with a new random value. Preflight fails if the name does not exist. Only the vault changes: syncing the new value to GitHub is a separate plan.

| Field | Required | Meaning |
| --- | --- | --- |
| `secret_name` | yes | existing secret |
| `env` | yes | vault environment |
| `generator.type` | yes | `password` |
| `generator.length` | no | default 32 |
| `rotation_mode` | no | `replace_existing` (default) |

```yaml
action: vault.secret.rotate
secret_name: SESSION_SECRET
env: staging
generator:
  type: password
  length: 48
```

## vault.secret.copy_many

Copies secrets from one vault environment to another, for example from `dev` to `staging`. Preflight fails if a source is missing or a target already exists; nothing is overwritten.

| Field | Required | Meaning |
| --- | --- | --- |
| `source_env` | yes | environment to copy from |
| `target_env` | yes | environment to copy to |
| `secrets` | yes | list of `source` / `target` name pairs |
| `create_mode` | no | `create_only` (default) |

```yaml
action: vault.secret.copy_many
source_env: dev
target_env: staging
secrets:
  - source: SMTP_USER
    target: SMTP_USER
  - source: SMTP_PASSWORD
    target: SMTP_PASSWORD
```

## vault.secret.diff_envs

Shows which of the listed names exist in each of two environments. Reads names only.

| Field | Required | Meaning |
| --- | --- | --- |
| `source_env` | yes | first environment |
| `target_env` | yes | second environment |
| `secrets` | yes | list of `name` entries |

```yaml
action: vault.secret.diff_envs
source_env: staging
target_env: production
secrets:
  - name: STRIPE_KEY
  - name: SESSION_SECRET
```

## vault.secret.import_file

Imports every entry of a dotenv file (`NAME=value` lines) that the human placed on the broker. The file must be readable by the broker account and not readable by group or others; the agent never reads or sends it. The names in the file become the secret names, and preflight fails if any already exists.

| Field | Required | Meaning |
| --- | --- | --- |
| `env` | yes | vault environment |
| `source_file` | yes | absolute path of the file on the broker |
| `create_mode` | no | `create_only` (default) |

```yaml
action: vault.secret.import_file
env: staging
source_file: /opt/secretops/import/staging.env
```

## github.env_secret.write

Copies one secret from the vault to a GitHub environment secret.

| Field | Required | Meaning |
| --- | --- | --- |
| `secret_name` | yes | vault secret to read |
| `env` | yes | vault environment |
| `github_owner` | yes | user or organization |
| `github_repo` | yes | repository |
| `github_environment` | yes | GitHub environment |
| `github_secret_name` | yes | name of the GitHub secret |
| `github_host` | no | default `github.com` |

```yaml
action: github.env_secret.write
secret_name: STRIPE_KEY
env: staging
github_owner: example-owner
github_repo: example-app
github_environment: staging
github_secret_name: STRIPE_KEY
```

## github.env_secret.sync_many

Copies several vault secrets to one GitHub environment. A failed run reports each name as written, not written, not attempted or unknown; written secrets stay written.

| Field | Required | Meaning |
| --- | --- | --- |
| `env` | yes | vault environment to read from |
| `github_owner` | yes | user or organization |
| `github_repo` | yes | repository |
| `github_environment` | yes | GitHub environment |
| `secrets` | yes | list of `source` (vault) / `target` (GitHub) name pairs |
| `github_host` | no | default `github.com` |

```yaml
action: github.env_secret.sync_many
env: staging
github_owner: example-owner
github_repo: example-app
github_environment: staging
secrets:
  - source: STRIPE_KEY
    target: STRIPE_KEY
  - source: SESSION_SECRET
    target: SESSION_SECRET
```

## github.env_secret.verify_many

Checks that names exist in a GitHub environment. GitHub never returns values, so this proves that a name exists, not which value it has.

| Field | Required | Meaning |
| --- | --- | --- |
| `github_owner` | yes | user or organization |
| `github_repo` | yes | repository |
| `github_environment` | yes | GitHub environment |
| `secrets` | yes | list of `name` entries |
| `github_host` | no | default `github.com` |

```yaml
action: github.env_secret.verify_many
github_owner: example-owner
github_repo: example-app
github_environment: staging
secrets:
  - name: STRIPE_KEY
  - name: SESSION_SECRET
```

More examples, including multi-secret backups, are in [`examples/recipes/`](../examples/recipes/).
