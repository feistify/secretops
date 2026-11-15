#!/bin/sh
# Builds and starts the trial container (docs/TRIAL.md). Needs Docker and
# network access for the build. Nothing is installed on this machine, and
# the container is removed when you leave it.
set -eu
# shellcheck disable=SC1007  # deliberate: empty CDPATH for this cd only
REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# Another Docker-compatible CLI can be named, for example DOCKER=nerdctl.
# Deliberately unquoted below, so it can hold a command with arguments.
DOCKER=${DOCKER:-docker}
if [ ! -t 0 ]; then
    echo "trial/run.sh needs a terminal; for a run without a person use: sh trial/check.sh" >&2
    exit 1
fi
if $DOCKER container inspect secretops-trial >/dev/null 2>&1; then
    echo "A trial container already exists. Open another shell in it with:" >&2
    echo "  $DOCKER exec -it -u you -w /home/you secretops-trial bash" >&2
    echo "or remove it with: $DOCKER rm -f secretops-trial" >&2
    exit 1
fi
echo "Building the trial image (the first time takes a few minutes) ..."
$DOCKER build -q -t secretops-trial -f "$REPO/trial/Dockerfile" "$REPO" >/dev/null
# --init only with Docker itself: other programs may need extra setup for it.
init=
[ "$DOCKER" != docker ] || init=--init
exec $DOCKER run --rm -it $init --name secretops-trial --hostname trial secretops-trial
