# Trial container: an installed broker with a human account, an agent
# account and stand-in vault and GitHub programs. For trying secretops only;
# see docs/TRIAL.md. Build from the repository root: sh trial/run.sh
FROM ubuntu:24.04 AS wheel
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        python3-venv ca-certificates >/dev/null
COPY pyproject.toml README.md LICENSE NOTICE /src/
COPY src /src/src
RUN python3 -m venv /build && /build/bin/pip wheel -q --no-deps -w /dist /src

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        python3-venv openssh-server openssh-client sudo procps iproute2 ca-certificates less >/dev/null \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash you && useradd -m -s /bin/bash agent \
    && printf 'you ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/trial && chmod 0440 /etc/sudoers.d/trial
COPY --chmod=0755 trial/infisical trial/gh /usr/bin/
COPY --chmod=0755 trial/status-server trial/answer /usr/local/lib/secretops-trial/
COPY --chmod=0644 trial/guide.txt tests/fake_infisical.py /usr/local/lib/secretops-trial/
COPY --chmod=0755 trial/trial-status trial/trial-guide trial/entrypoint /usr/local/bin/
COPY --chmod=0755 scripts /src/scripts
COPY --from=wheel /dist /src/dist
# The same installer as a real installation; then the stand-in vault is
# configured through the normal prompts and checked.
RUN cd /src && env SUDO_USER=you bash scripts/install.sh --wheel /src/dist/*.whl \
        --agent-user agent --infisical-domain http://127.0.0.1:8086 --no-start >/dev/null \
    && install -d -o secretops-broker -g secretops-broker -m 0700 /var/lib/secretops-trial
RUN (runuser -u nobody -- /usr/local/lib/secretops-trial/status-server &) && sleep 1 \
    && /usr/local/lib/secretops-trial/answer 'Vault URL=http://127.0.0.1:8086' \
        'Project ID=trial-project' 'Vault folder path=/' 'access token=trial-token-not-a-secret' \
        -- secretops-configure > /tmp/configure.out \
    && grep -q '^OK vault connection works' /tmp/configure.out && rm /tmp/configure.out \
    || { cat /tmp/configure.out; exit 1; }
COPY --chown=agent:agent trial/recipes /home/agent/recipes
ENTRYPOINT ["/usr/local/bin/entrypoint"]
