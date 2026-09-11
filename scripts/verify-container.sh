#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
verification_project="leadscout-verify-$$"
cleanup() {
    docker compose -p "$verification_project" -f docker-compose.verify.yml down --volumes --remove-orphans
}
trap cleanup EXIT
docker compose -p "$verification_project" -f docker-compose.verify.yml config --quiet
docker compose -p "$verification_project" -f docker-compose.verify.yml build
docker compose -p "$verification_project" -f docker-compose.verify.yml up --abort-on-container-exit --exit-code-from verify
