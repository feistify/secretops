# Security policy

secretops brokers access to secrets for coding agents, so a flaw in its boundary matters more than in most tools. Please report it privately.

## Supported versions

secretops is early-stage software. Only the latest release and the `main` branch receive security fixes. Production writes are blocked by the default policy; see the limits in [README.md](README.md) and [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue, discussion or pull request for a suspected vulnerability.

Include the version or commit, how secretops is installed (for example the local broker from [docs/INSTALL.md](docs/INSTALL.md)), the steps to reproduce, and what an attacker gains. Use synthetic values only: never include real tokens, secret values, private keys or private hostnames in a report.

Especially relevant:

- an agent reading or obtaining a secret value, the vault token or the broker's GitHub login;
- an agent approving, changing or replaying a plan, or bypassing policy or destination binding;
- a secret value in output, audit records, plans, command arguments or agent-readable files;
- privilege problems in `scripts/install.sh` or `secretops-configure`.

Vulnerabilities in Infisical, the GitHub CLI or OpenSSH belong to those projects, unless secretops uses them unsafely.

## What to expect

secretops is maintained by one person, so handling is best effort. The aim is to acknowledge a report within a week, agree on a fix and a disclosure date with the reporter, and credit the reporter in the advisory unless they prefer otherwise.
