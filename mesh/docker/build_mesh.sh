#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${LOG_DIR:-${SCRIPT_DIR}/logs}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/build_mesh_$(date +%Y%m%d_%H%M%S).log}"

mkdir -p "${LOG_DIR}"
# Mirror all stdout/stderr to terminal and log file.
exec > >(tee -a "${LOG_FILE}") 2>&1

DOCKERFILE_PATH="${SCRIPT_DIR}/Dockerfile_mesh"
BASE_IMAGE="${BASE_IMAGE:-rocm/atom-dev:vllm-latest}"
IMAGE_REPO="${IMAGE_REPO:-rocm/atom-mesh}"
IMAGE_TAG="${IMAGE_TAG:-${IMAGE_REPO}:latest}"
MAX_JOBS="${MAX_JOBS:-64}"
ATOM_REPO="${ATOM_REPO:-https://github.com/Jasen2201/ATOM.git}"
ATOM_BRANCH="${ATOM_BRANCH:-pd_distributed}"
INSTALL_RDMA="${INSTALL_RDMA:-1}"
RDMA_LIB_PATH="${RDMA_LIB_PATH:-/usr/local/lib/libbnxt_re-rdmav34.so}"
INSTALL_MOONCAKE="${INSTALL_MOONCAKE:-1}"
MOONCAKE_REPO="${MOONCAKE_REPO:-https://github.com/kvcache-ai/Mooncake.git}"
MOONCAKE_COMMIT="${MOONCAKE_COMMIT:-}"
INSTALL_SMG="${INSTALL_SMG:-1}"
MESH_REPO="${MESH_REPO:-https://github.com/Jasen2201/MESH.git}"
MESH_BRANCH="${MESH_BRANCH:-main}"
INSTALL_SGLANG="${INSTALL_SGLANG:-1}"
SGLANG_REPO="${SGLANG_REPO:-https://github.com/sgl-project/sglang.git}"
SGLANG_BRANCH="${SGLANG_BRANCH:-main}"
SGL_GPU_ARCH="${SGL_GPU_ARCH:-gfx942}"
PULL_BASE_IMAGE="${PULL_BASE_IMAGE:-1}"
BUILD_NO_CACHE="${BUILD_NO_CACHE:-1}"

print_banner() {
  echo "============================================================"
  echo "$1"
  echo "============================================================"
}

print_banner "Build MESH image on top of ATOM+vLLM base image"
echo "Log file        : ${LOG_FILE}"
echo "Dockerfile      : ${DOCKERFILE_PATH}"
echo "Build context   : ${SCRIPT_DIR}"
echo "Target image    : ${IMAGE_TAG}"
echo "Base image      : ${BASE_IMAGE}"
echo "ATOM repo       : ${ATOM_REPO}"
echo "ATOM branch     : ${ATOM_BRANCH}"
echo "MAX_JOBS        : ${MAX_JOBS}"
echo "INSTALL_RDMA    : ${INSTALL_RDMA}"
echo "RDMA_LIB_PATH   : ${RDMA_LIB_PATH}"
echo "INSTALL_MOONCAKE: ${INSTALL_MOONCAKE}"
echo "MOONCAKE_REPO   : ${MOONCAKE_REPO}"
echo "MOONCAKE_COMMIT : ${MOONCAKE_COMMIT:-latest}"
echo "INSTALL_SMG     : ${INSTALL_SMG}"
echo "MESH_REPO       : ${MESH_REPO}"
echo "MESH_BRANCH     : ${MESH_BRANCH}"
echo "INSTALL_SGLANG  : ${INSTALL_SGLANG}"
echo "SGLANG_REPO     : ${SGLANG_REPO}"
echo "SGLANG_BRANCH   : ${SGLANG_BRANCH}"
echo "SGL_GPU_ARCH    : ${SGL_GPU_ARCH}"
echo "BUILD_NO_CACHE  : ${BUILD_NO_CACHE}"
echo
echo "Build plan:"
echo "  Step 1/5: (optional) pull base image"
echo "  Step 2/5: check/remove existing target image"
echo "  Step 3/5: (optional) prepare RDMA libraries"
echo "  Step 4/5: build image from Dockerfile_mesh"
echo "  Step 5/5: print final image info"
echo

if [[ "${PULL_BASE_IMAGE}" == "1" ]]; then
  print_banner "Step 1/5 - Pull base image: ${BASE_IMAGE}"
  docker pull "${BASE_IMAGE}"
else
  print_banner "Step 1/5 - Skip base image pull (PULL_BASE_IMAGE=${PULL_BASE_IMAGE})"
fi

print_banner "Step 2/5 - Check whether target image already exists"
if docker image inspect "${IMAGE_TAG}" >/dev/null 2>&1; then
  echo "Target image already exists: ${IMAGE_TAG}"
  docker image inspect "${IMAGE_TAG}" --format 'Existing image -> ID={{.Id}}  Created={{.Created}}'
  echo "Removing existing target image: ${IMAGE_TAG}"
  docker image rm -f "${IMAGE_TAG}"
else
  echo "Target image does not exist yet: ${IMAGE_TAG}"
fi
echo

# Prepare RDMA libraries for build context if requested
RDMA_STAGED_DIR=""
if [[ "${INSTALL_RDMA}" == "1" ]]; then
  print_banner "Prepare RDMA - Copy host rdma-core v39 libraries into build context"
  RDMA_STAGED_DIR="${SCRIPT_DIR}/rdma_host_libs"
  mkdir -p "${RDMA_STAGED_DIR}/libibverbs"

  # Core libraries
  RDMA_HOST_DIR="/usr/lib/x86_64-linux-gnu"
  for lib in libibverbs.so.1.14.39.0 librdmacm.so.1.3.39.0; do
    if [[ ! -f "${RDMA_HOST_DIR}/${lib}" ]]; then
      echo "ERROR: ${lib} not found at ${RDMA_HOST_DIR}/${lib}"
      exit 1
    fi
    cp "${RDMA_HOST_DIR}/${lib}" "${RDMA_STAGED_DIR}/"
  done
  # Recreate symlinks
  cd "${RDMA_STAGED_DIR}"
  ln -sf libibverbs.so.1.14.39.0  libibverbs.so.1
  ln -sf libibverbs.so.1          libibverbs.so
  ln -sf librdmacm.so.1.3.39.0   librdmacm.so.1
  ln -sf librdmacm.so.1           librdmacm.so
  cd -

  # Broadcom bnxt_re provider plugin
  if [[ -f "${RDMA_LIB_PATH}" ]]; then
    cp "${RDMA_LIB_PATH}" "${RDMA_STAGED_DIR}/libibverbs/libbnxt_re-rdmav34.so"
    echo "Staged bnxt_re provider: ${RDMA_LIB_PATH}"
  else
    echo "WARNING: RDMA provider not found at ${RDMA_LIB_PATH}, skipping bnxt_re."
  fi

  echo "Staged RDMA libraries:"
  ls -lhR "${RDMA_STAGED_DIR}"
fi

print_banner "Step 3/5 - Build target image: ${IMAGE_TAG}"
NO_CACHE_FLAG=""
if [[ "${BUILD_NO_CACHE}" == "1" ]]; then
  NO_CACHE_FLAG="--no-cache"
fi

DOCKER_BUILDKIT=1 docker build \
  ${NO_CACHE_FLAG} \
  -f "${DOCKERFILE_PATH}" \
  -t "${IMAGE_TAG}" \
  --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "ATOM_REPO=${ATOM_REPO}" \
  --build-arg "ATOM_BRANCH=${ATOM_BRANCH}" \
  --build-arg "MAX_JOBS=${MAX_JOBS}" \
  --build-arg "INSTALL_RDMA=${INSTALL_RDMA}" \
  --build-arg "INSTALL_MOONCAKE=${INSTALL_MOONCAKE}" \
  --build-arg "MOONCAKE_REPO=${MOONCAKE_REPO}" \
  --build-arg "MOONCAKE_COMMIT=${MOONCAKE_COMMIT}" \
  --build-arg "INSTALL_SMG=${INSTALL_SMG}" \
  --build-arg "MESH_REPO=${MESH_REPO}" \
  --build-arg "MESH_BRANCH=${MESH_BRANCH}" \
  --build-arg "INSTALL_SGLANG=${INSTALL_SGLANG}" \
  --build-arg "SGLANG_REPO=${SGLANG_REPO}" \
  --build-arg "SGLANG_BRANCH=${SGLANG_BRANCH}" \
  --build-arg "SGL_GPU_ARCH=${SGL_GPU_ARCH}" \
  "$@" \
  "${SCRIPT_DIR}"

# Clean up staged RDMA libraries from build context
if [[ -n "${RDMA_STAGED_DIR}" && -d "${RDMA_STAGED_DIR}" ]]; then
  rm -rf "${RDMA_STAGED_DIR}"
  echo "Cleaned up staged RDMA libraries."
fi

print_banner "Step 4/5 - Build completed"
docker image inspect "${IMAGE_TAG}" --format 'Image={{.RepoTags}}  ID={{.Id}}  Created={{.Created}}'
