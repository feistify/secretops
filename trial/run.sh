#!/bin/sh
# Builds and starts the trial container (docs/TRIAL.md). Needs Docker and
# network access for the build. Nothing is installed on this machine, and
# the container is removed when you leave it.
set -eu
# shellcheck disable=SC1007  # deliberate: empty CDPATH for this cd only
REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -t 0 ]; then
    echo "trial/run.sh needs a terminal; for a run without a person use: sh trial/check.sh" >&2
    exit 1
fi
if [ -n "$(docker ps -q -f name='^secretops-trial$')" ]; then
    echo "The trial container is already running. Open another shell in it with:" >&2
    echo "  docker exec -it -u you -w /home/you secretops-trial bash" >&2
    exit 1
fi
echo "Building the trial image (the first time takes a few minutes) ..."
docker build -q -t secretops-trial -f "$REPO/trial/Dockerfile" "$REPO" >/dev/null
exec docker run --rm -it --init --name secretops-trial --hostname trial secretops-trial
