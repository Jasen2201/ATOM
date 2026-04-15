#!/usr/bin/env bash
# =============================================================================
# Script 0: Start Docker Container
#
# Run this on the HOST machine (not inside docker).
# Start this on EACH node that will run prefill or decode.
#
# Usage:
#   bash 0_setup_docker.sh
#
# Environment overrides:
#   DOCKER_IMAGE    - docker image (default: rocm/atom-mesh:latest)
#   CONTAINER_NAME  - container name (default: mesh_dev)
# =============================================================================
set -euo pipefail

DOCKER_IMAGE="${DOCKER_IMAGE:-rocm/atom-mesh:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-mesh_dev}"

echo ""
echo "============================================================"
echo "  Starting Docker Container: ${CONTAINER_NAME}"
echo "============================================================"
echo " Image: ${DOCKER_IMAGE}"
echo "============================================================"

# Check if container already exists
status=$(docker inspect -f '{{.State.Status}}' "${CONTAINER_NAME}" 2>/dev/null || echo "not_found")

if [[ "${status}" == "running" ]]; then
    echo "[ok] Container '${CONTAINER_NAME}' already running."
    echo "     Enter with: docker exec -it ${CONTAINER_NAME} bash"
    exit 0
elif [[ "${status}" != "not_found" ]]; then
    echo "[warn] Container '${CONTAINER_NAME}' exists but status='${status}'. Removing..."
    docker rm -f "${CONTAINER_NAME}" || true
fi

docker run -d \
    --name "${CONTAINER_NAME}" \
    --network host \
    --ipc host \
    --privileged \
    --device /dev/kfd \
    --device /dev/dri \
    -v /mnt:/mnt \
    -v /it-share:/it-share \
    --group-add video \
    --group-add render \
    --cap-add IPC_LOCK \
    --cap-add NET_ADMIN \
    "${DOCKER_IMAGE}" \
    sleep infinity

echo "[ok] Container '${CONTAINER_NAME}' started."
echo ""
echo "Enter the container with:"
echo "  docker exec -it ${CONTAINER_NAME} bash"
