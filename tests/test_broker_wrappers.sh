#!/bin/sh
# Scripted boundary tests for the SSH forced-command wrappers.
#
# The production scripts hardcode their security-relevant paths and read
# nothing from the environment. These tests exercise the real script bytes
# with only the two CONFIG lines rewritten to point at a temp sandbox, so the
# validation and dispatch logic under test is exactly what ships.
#
# Run: sh tests/test_broker_wrappers.sh
# Note: no set -e here — denial cases return non-zero by design and are
# asserted individually; the final exit reflects the aggregate.
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../scripts" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

RECIPE_DIR="$WORK/recipes"
FAKE_BROKER="$WORK/fake-broker"
CALLLOG="$WORK/broker-calls"

cat > "$FAKE_BROKER" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$CALLLOG"
echo "BROKER-CALLED: \$*"
EOF
chmod +x "$FAKE_BROKER"

# Build a testable copy of secretops-agent with the two CONFIG lines rewritten.
AGENT="$WORK/secretops-agent"
sed \
    -e "s#^BROKER_BIN=.*#BROKER_BIN=$FAKE_BROKER#" \
    -e "s#^RECIPE_DIR=.*#RECIPE_DIR=$RECIPE_DIR#" \
    "$SCRIPT_DIR/secretops-agent" > "$AGENT"
chmod +x "$AGENT"

# Build a testable copy of secretops-broker pointing at a temp env file.
BROKER_ENV="$WORK/broker.env"
BROKER="$WORK/secretops-broker"
sed -e "s#^BROKER_ENV=.*#BROKER_ENV=$BROKER_ENV#" \
    "$SCRIPT_DIR/secretops-broker" > "$BROKER"
chmod +x "$BROKER"

pass=0
fail=0
check() { # description  expected_rc  actual_rc
    if [ "$2" = "$3" ]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        echo "FAIL: $1 (expected rc=$2, got rc=$3)"
    fi
}

agent() { # SSH_ORIGINAL_COMMAND, optional stdin
    SSH_ORIGINAL_COMMAND="$1" sh "$AGENT" > "$WORK/out" 2>&1
}

echo "== secretops-agent: allowed shapes =="
: > "$CALLLOG"
agent "secretops version" </dev/null; check "version allowed" 0 $?
agent "actions" </dev/null; check "actions allowed (bare)" 0 $?
agent "secretops plan --file import.yaml --save" </dev/null; check "plan by name allowed" 0 $?
grep -q "plan --file $RECIPE_DIR/import.yaml --save" "$CALLLOG" \
    && check "plan resolved into RECIPE_DIR" 0 0 || check "plan resolved into RECIPE_DIR" 0 1
agent "secretops show-plan --id deadbeef1234" </dev/null; check "show-plan allowed" 0 $?
agent "preflight-plan --id deadbeef1234" </dev/null; check "preflight-plan allowed" 0 $?
agent "execute-plan --id deadbeef1234" </dev/null; check "execute-plan allowed" 0 $?

echo "== secretops-agent: blocker 1 (arbitrary --file path) =="
agent "secretops plan --file /etc/passwd --save" </dev/null; check "absolute --file denied" 1 $?
agent "plan --file ../../etc/passwd --save" </dev/null; check "traversal --file denied" 1 $?
agent "plan --file ../secret.yaml --save" </dev/null; check "dotdot recipe denied" 1 $?
agent "plan --file sub/import.yaml --save" </dev/null; check "slash in recipe denied" 1 $?
agent "plan --file import.txt --save" </dev/null; check "non-yaml recipe denied" 1 $?
agent "plan --file import.yaml" </dev/null; check "plan without --save denied" 1 $?
agent "plan --file import.yaml --save --extra x" </dev/null; check "plan extra args denied" 1 $?

echo "== secretops-agent: approval + traversal + injection =="
agent "approve-plan --id deadbeef" </dev/null; check "approve-plan denied" 1 $?
agent "reject-plan --id deadbeef" </dev/null; check "reject-plan denied" 1 $?
agent "prepare-add --name DUMMY_SECRET --env dev" </dev/null; check "prepare-add (human endpoint) denied" 1 $?
agent "review-add --id deadbeef" </dev/null; check "review-add (human endpoint) denied" 1 $?
echo '{"value":"x"}' | agent "submit-add"; check "submit-add (human endpoint) denied" 1 $?
agent "add DUMMY_SECRET --env dev" </dev/null; check "add denied" 1 $?
agent "show-plan --id ../../broker" </dev/null; check "traversal plan id denied" 1 $?
agent "show-plan --id a b" </dev/null; check "multi-token plan id denied" 1 $?
agent "plan; rm -rf /" </dev/null; check "semicolon denied" 1 $?
agent 'show-plan --id $(cat /etc/passwd)' </dev/null; check "command substitution denied" 1 $?
agent "version --help" </dev/null; check "version with args denied" 1 $?
agent "" </dev/null; check "empty command denied" 1 $?
agent "bash" </dev/null; check "bash denied" 1 $?

echo "== secretops-agent: write-recipe =="
printf 'action: vault.secret.import_file\n' | SSH_ORIGINAL_COMMAND="write-recipe good.yaml" sh "$AGENT" >/dev/null 2>&1
check "write-recipe good name" 0 $?
[ -f "$RECIPE_DIR/good.yaml" ] && check "recipe written" 0 0 || check "recipe written" 0 1
mode=$(stat -c '%a' "$RECIPE_DIR/good.yaml" 2>/dev/null || stat -f '%Lp' "$RECIPE_DIR/good.yaml")
[ "$mode" = "600" ] && check "recipe mode 0600" 0 0 || check "recipe mode 0600 (got $mode)" 0 1
printf 'x' | SSH_ORIGINAL_COMMAND="write-recipe ../evil.yaml" sh "$AGENT" >/dev/null 2>&1
check "write-recipe traversal denied" 1 $?
printf 'x' | SSH_ORIGINAL_COMMAND="write-recipe .hidden.yaml" sh "$AGENT" >/dev/null 2>&1
check "write-recipe dotfile denied" 1 $?
# Oversize: cap is 65536; feed 70000 bytes, expect rejection and no file.
head -c 70000 /dev/zero | tr '\0' 'a' | SSH_ORIGINAL_COMMAND="write-recipe big.yaml" sh "$AGENT" >/dev/null 2>&1
check "write-recipe oversize rejected" 1 $?
[ ! -f "$RECIPE_DIR/big.yaml" ] && check "oversize left no file" 0 0 || check "oversize left no file" 0 1

echo "== secretops-broker: env-file permission gate =="
printf 'SECRETOPS_BIN=%s\n' "$FAKE_BROKER" > "$BROKER_ENV"
chmod 644 "$BROKER_ENV"; sh "$BROKER" version >/dev/null 2>&1; check "0644 env refused" 1 $?
chmod 600 "$BROKER_ENV"; sh "$BROKER" version >/dev/null 2>&1; check "0600 env accepted" 0 $?
# Client-supplied SECRETOPS_BIN must be ignored (only broker.env may set it).
EVIL="$WORK/evil-bin"; printf '#!/bin/sh\necho EVIL\n' > "$EVIL"; chmod +x "$EVIL"
out=$(SECRETOPS_BIN="$EVIL" sh "$BROKER" version 2>&1 || true)
case "$out" in *EVIL*) check "client SECRETOPS_BIN override ignored" 0 1 ;; *) check "client SECRETOPS_BIN override ignored" 0 0 ;; esac

# SECRETOPS_INFISICAL_BIN picks the CLI that receives the vault token, so it
# may come only from broker.env. The probe prints what secretops would see.
PROBE="$WORK/env-probe"
printf '#!/bin/sh\necho "INFISICAL_BIN=${SECRETOPS_INFISICAL_BIN-unset}"\n' > "$PROBE"; chmod +x "$PROBE"
printf 'SECRETOPS_BIN=%s\n' "$PROBE" > "$BROKER_ENV"; chmod 600 "$BROKER_ENV"
out=$(SECRETOPS_INFISICAL_BIN=/tmp/planted-infisical sh "$BROKER" version 2>&1 || true)
[ "$out" = "INFISICAL_BIN=unset" ] \
    && check "inherited SECRETOPS_INFISICAL_BIN cleared when broker.env omits it" 0 0 \
    || check "inherited SECRETOPS_INFISICAL_BIN cleared when broker.env omits it (got: $out)" 0 1
printf 'SECRETOPS_BIN=%s\nSECRETOPS_INFISICAL_BIN=/usr/local/bin/infisical\n' "$PROBE" > "$BROKER_ENV"; chmod 600 "$BROKER_ENV"
out=$(SECRETOPS_INFISICAL_BIN=/tmp/planted-infisical sh "$BROKER" version 2>&1 || true)
[ "$out" = "INFISICAL_BIN=/usr/local/bin/infisical" ] \
    && check "broker.env SECRETOPS_INFISICAL_BIN wins over the caller's" 0 0 \
    || check "broker.env SECRETOPS_INFISICAL_BIN wins over the caller's (got: $out)" 0 1

# SECRETOPS_GH_BIN picks the program that receives secret values on stdin and
# the broker's GitHub login, so it may also come only from broker.env.
printf '#!/bin/sh\necho "GH_BIN=${SECRETOPS_GH_BIN-unset}"\n' > "$PROBE"; chmod +x "$PROBE"
printf 'SECRETOPS_BIN=%s\n' "$PROBE" > "$BROKER_ENV"; chmod 600 "$BROKER_ENV"
out=$(SECRETOPS_GH_BIN=/tmp/planted-gh sh "$BROKER" version 2>&1 || true)
[ "$out" = "GH_BIN=unset" ] \
    && check "inherited SECRETOPS_GH_BIN cleared when broker.env omits it" 0 0 \
    || check "inherited SECRETOPS_GH_BIN cleared when broker.env omits it (got: $out)" 0 1
printf 'SECRETOPS_BIN=%s\nSECRETOPS_GH_BIN=/usr/bin/gh\n' "$PROBE" > "$BROKER_ENV"; chmod 600 "$BROKER_ENV"
out=$(SECRETOPS_GH_BIN=/tmp/planted-gh sh "$BROKER" version 2>&1 || true)
[ "$out" = "GH_BIN=/usr/bin/gh" ] \
    && check "broker.env SECRETOPS_GH_BIN wins over the caller's" 0 0 \
    || check "broker.env SECRETOPS_GH_BIN wins over the caller's (got: $out)" 0 1

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
