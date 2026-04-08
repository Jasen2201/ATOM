#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# run_dev.sh — Start a dev container with local MESH source mounted.
#
# Usage:
#   bash run_dev.sh                    # interactive bash, auto-install Rust
#   bash run_dev.sh cargo build        # run a command directly
#   IMAGE_TAG=myimage:tag bash run_dev.sh
#
# The local MESH repo (parent of docker/) is volume-mounted to /app/mesh.
# Rust toolchain is installed on first start (~30s) and cached via a named
# Docker volume so subsequent starts are instant.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MESH_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_TAG="${IMAGE_TAG:-rocm/atom-mesh:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-mesh-dev}"
RUST_VERSION="${RUST_VERSION:-1.94.0}"

# Named volume to persist Rust toolchain + cargo cache across container restarts
CARGO_VOLUME="${CARGO_VOLUME:-mesh-dev-cargo}"

echo "============================================================"
echo "MESH Dev Container"
echo "============================================================"
echo "Image          : ${IMAGE_TAG}"
echo "Container      : ${CONTAINER_NAME}"
echo "MESH source    : ${MESH_ROOT} -> /app/mesh"
echo "Rust version   : ${RUST_VERSION}"
echo "Cargo volume   : ${CARGO_VOLUME} (persists toolchain)"
echo "============================================================"

# Build the entrypoint script that installs Rust if missing
ENTRYPOINT_SCRIPT=$(cat <<'INNER_EOF'
#!/bin/bash
set -e

RUST_VERSION="${RUST_VERSION:-1.94.0}"

# Install Rust if not already present (cached in volume)
if ! command -v cargo &>/dev/null; then
    echo ">>> Installing Rust ${RUST_VERSION} (first run, ~30s) ..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain "${RUST_VERSION}" --profile minimal 2>&1
fi

# Source cargo env
. "$HOME/.cargo/env"
echo ">>> Rust: $(rustc --version), Cargo: $(cargo --version)"

# If no command given, drop into bash
if [ $# -eq 0 ]; then
    echo ""
    echo ">>> MESH source mounted at /app/mesh"
    echo ">>> To build:  cd /app/mesh && cargo build --release"
    echo ">>> To install: cp target/release/mesh /usr/local/bin/mesh"
    echo ""
    exec bash
else
    exec "$@"
fi
INNER_EOF
)

# Remove existing container with same name if stopped
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "Removing existing stopped container: ${CONTAINER_NAME}"
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
fi

docker run -it \
    --name "${CONTAINER_NAME}" \
    --network host \
    --device /dev/kfd \
    --device /dev/dri \
    --group-add video \
    --cap-add=SYS_PTRACE \
    --security-opt seccomp=unconfined \
    --shm-size 64G \
    -v "${MESH_ROOT}:/app/mesh" \
    -v "${CARGO_VOLUME}:/root/.cargo" \
    -v "${CARGO_VOLUME}-rustup:/root/.rustup" \
    -e "RUST_VERSION=${RUST_VERSION}" \
    -w /app/mesh \
    "$@" \
    "${IMAGE_TAG}" \
    bash -c "${ENTRYPOINT_SCRIPT}"
