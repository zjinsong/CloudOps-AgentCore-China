#!/usr/bin/env bash
set -euo pipefail
image_uri=$(python -c 'from scripts.deploy import load; print(load()["image_uri"])')
registry=${image_uri%%/*}
aws ecr get-login-password --region cn-northwest-1 | docker login --username AWS --password-stdin "$registry"
docker buildx build --platform linux/arm64 --provenance=false --push -t "$image_uri" .
