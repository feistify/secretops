# Helper for docs/demo/demo.tape. Sourced in a hidden step; not a general tool.
#
# It writes the two demo recipes (names and metadata only), switches the
# prompt between "you" and "agent", and remembers the last plan id so the
# recording can show `--id $PLAN` instead of a pasted id.
#
# Needs DEMO_OWNER and DEMO_REPO: a test GitHub repository with a `dev`
# environment. Their names are visible in the recording.

: "${DEMO_OWNER:?set DEMO_OWNER to the test repository owner}"
: "${DEMO_REPO:?set DEMO_REPO to the test repository name}"

DEMO_DIR=$(mktemp -d)

cat > "$DEMO_DIR/demo-sync.yaml" <<YAML
action: github.env_secret.sync_many
env: dev
github_owner: $DEMO_OWNER
github_repo: $DEMO_REPO
github_environment: dev
secrets:
  - source: DEMO_API_KEY
    target: DEMO_API_KEY
YAML

cat > "$DEMO_DIR/demo-verify.yaml" <<YAML
action: github.env_secret.verify_many
github_owner: $DEMO_OWNER
github_repo: $DEMO_REPO
github_environment: dev
secrets:
  - name: DEMO_API_KEY
YAML

cd "$DEMO_DIR" || return 1

as_you() { PS1='\[\e[1;32m\]you\[\e[0m\] $ '; }
as_agent() { PS1='\[\e[1;36m\]agent\[\e[0m\] $ '; }

# Same command as the installed secretops-local; after `plan` it also keeps
# the printed plan id in $PLAN.
secretops-local() {
    command secretops-local "$@" | tee "$DEMO_DIR/last.out"
    if [ "${1:-}" = plan ]; then
        PLAN=$(sed -n 's/^- plan_id: //p' "$DEMO_DIR/last.out")
    fi
}

as_you
