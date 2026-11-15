#!/bin/sh
# Builds the trial image and walks the whole flow from trial/guide.txt
# without a person: entry, plan, early execution, approval, execution,
# verification. Needs Docker and network access for the build.
#
#   sh trial/check.sh
#   DOCKER="sudo nerdctl" sh trial/check.sh    # another Docker-compatible CLI
set -eu
# shellcheck disable=SC1007  # deliberate: empty CDPATH for this cd only
REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
NAME=secretops-trial-check
VALUE=made-up-trial-value-0123456789
# Deliberately unquoted below, so it can hold a command with arguments.
DOCKER=${DOCKER:-docker}
$DOCKER build -q -t secretops-trial -f "$REPO/trial/Dockerfile" "$REPO" >/dev/null
$DOCKER rm -f "$NAME" >/dev/null 2>&1 || true
# No --rm: some programs refuse it with -d. The traps remove the container,
# also after Ctrl-C (sh does not run the EXIT trap on a signal by itself).
trap '$DOCKER rm -f "$NAME" >/dev/null 2>&1' EXIT
trap 'exit 130' INT TERM
$DOCKER run -d --name "$NAME" secretops-trial sleep 600 >/dev/null
sleep 1

OUT=$(mktemp)
fail=0
check() { # label, expected (pass|deny), command...
    label=$1 expected=$2
    shift 2
    if "$@" >>"$OUT" 2>&1 </dev/null; then got=pass; else got=deny; fi
    if [ "$got" = "$expected" ]; then
        echo "PASS: $label"
    else
        echo "FAIL: $label (got $got)"; tail -n 5 "$OUT"; fail=1
    fi
}
you() { $DOCKER exec -u you -w /home/you "$NAME" "$@"; }
agent() { $DOCKER exec -u agent -w /home/agent "$NAME" "$@"; }
broker() { you sudo -u secretops-broker /usr/local/bin/secretops-broker "$@"; }

check "vault connection check works" pass you sh -c 'sudo secretops-configure --check | grep -q "^OK vault connection works"'
check "human enters a value" pass you /usr/local/lib/secretops-trial/answer \
    "Value (hidden)=$VALUE" 'Save?=y' -- secretops add DEMO_API_KEY --env dev
check "agent uploads the recipe" pass agent sh -c 'secretops-local write-recipe sync.yaml < recipes/sync.yaml'
check "agent saves a plan" pass agent sh -c 'secretops-local plan --file sync.yaml --save > plan.out'
PLAN=$(agent sed -n 's/^- plan_id: //p' plan.out)
check "agent shows the plan" pass agent secretops-local show-plan --id "$PLAN"
check "execution before approval refused" deny agent secretops-local execute-plan --id "$PLAN"
check "plan still waits for approval" pass sh -c "$DOCKER exec -u agent $NAME secretops-local show-plan --id $PLAN | grep -q 'status: planned'"
check "agent cannot approve" deny agent secretops-local approve-plan --id "$PLAN"
check "agent cannot read the stand-in vault" deny agent cat /var/lib/secretops-trial/vault.json
check "agent cannot read broker.env" deny agent cat /opt/secretops/broker.env
check "agent cannot use the stand-in vault program" deny agent env INFISICAL_TOKEN=trial-token-not-a-secret /usr/bin/infisical --domain http://127.0.0.1:8086 run --env=dev --projectId=trial-project --path=/ -- true
check "human approves" pass broker approve-plan --id "$PLAN"
check "agent executes the approved plan" pass agent secretops-local execute-plan --id "$PLAN"
check "second execution refused" deny agent secretops-local execute-plan --id "$PLAN"
check "name reached the stand-in GitHub" pass sh -c "$DOCKER exec -u you $NAME sudo trial-status | grep -A1 'Stand-in GitHub' | grep -q 'dev: DEMO_API_KEY'"
check "verification without approval" pass agent sh -c \
    'secretops-local write-recipe verify.yaml < recipes/verify.yaml && secretops-local plan --file verify.yaml --save > verify.out && secretops-local execute-plan --id "$(sed -n "s/^- plan_id: //p" verify.out)"'
check "generation needs approval, then runs" pass sh -c "
    set -e
    $DOCKER exec -u agent -w /home/agent $NAME sh -c 'secretops-local write-recipe generate.yaml < recipes/generate.yaml && secretops-local plan --file generate.yaml --save > generate.out'
    id=\$($DOCKER exec -u agent -w /home/agent $NAME sed -n 's/^- plan_id: //p' generate.out)
    $DOCKER exec -u you $NAME sudo -u secretops-broker /usr/local/bin/secretops-broker approve-plan --id \$id
    $DOCKER exec -u agent $NAME secretops-local execute-plan --id \$id"
check "status lists the generated name" pass sh -c "$DOCKER exec -u you $NAME sudo trial-status | grep -q 'dev: DEMO_API_KEY, TRIAL_GENERATED_PASSWORD'"
$DOCKER exec "$NAME" sh -c 'cat /opt/secretops/audit/secretops.jsonl /opt/secretops/plans/*; trial-status' >>"$OUT" 2>&1
check "audit has events" pass sh -c "$DOCKER exec $NAME test -s /opt/secretops/audit/secretops.jsonl"
check "the value appears in no output, plan or audit record" deny grep -q "$VALUE" "$OUT"
rm -f "$OUT"
[ "$fail" = 0 ] && echo "All trial checks passed."
exit "$fail"
