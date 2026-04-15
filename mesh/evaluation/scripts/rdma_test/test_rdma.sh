#!/usr/bin/env bash
# =============================================================================
# Mooncake RDMA connectivity test (loopback / cross-node)
# =============================================================================
# Usage:
#   1) Single-node loopback:
#        bash test_rdma.sh loopback
#
#   2) Cross-node (two steps):
#        Node A:  bash test_rdma.sh server
#        Node B:  bash test_rdma.sh client <rpc_port from server output>
#
# Environment variables (all optional):
#   SERVER_RDMA_IP   - Server RDMA IP          (default: auto-detect)
#   CLIENT_RDMA_IP   - Client RDMA IP          (default: auto-detect)
#   LOOPBACK_IP      - Loopback IP override    (default: auto-detect)
#   RDMA_DEVICE      - RDMA device filter      (default: empty = all devices)
#   BUF_SIZE         - Transfer buffer size     (default: 4096)
#   TIMEOUT          - Client timeout in sec    (default: 30)
# =============================================================================
set -euo pipefail

MODE="${1:-}"
if [[ -z "${MODE}" ]]; then
    echo "Usage:"
    echo "  bash test_rdma.sh server                  # Start server"
    echo "  bash test_rdma.sh client <rpc_port>       # Connect to server"
    echo "  bash test_rdma.sh loopback                # Single-node self-test"
    exit 1
fi

# ---- Configuration ----
SERVER_RDMA_IP="${SERVER_RDMA_IP:-}"
CLIENT_RDMA_IP="${CLIENT_RDMA_IP:-}"
BUF_SIZE="${BUF_SIZE:-4096}"
TIMEOUT="${TIMEOUT:-30}"
RDMA_DEVICE="${RDMA_DEVICE:-}"
export MC_GID_INDEX="${MC_GID_INDEX:-2}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

# ---- LD_LIBRARY_PATH ----
MOONCAKE_LIB="${MOONCAKE_LIB:-/opt/venv/lib/python3.12/site-packages/mooncake}"
export LD_LIBRARY_PATH="${MOONCAKE_LIB}:/opt/rocm/lib:${LD_LIBRARY_PATH:-}"

# ---- Auto-detect RDMA IP from host interfaces ----
auto_detect_rdma_ip() {
    # If RDMA_DEVICE is set, try to get the IP from its associated net device first
    if [[ -n "${RDMA_DEVICE}" ]]; then
        local ndev
        ndev="$(cat /sys/class/infiniband/${RDMA_DEVICE}/ports/1/gid_attrs/ndevs/0 2>/dev/null || true)"
        if [[ -n "${ndev}" ]]; then
            local dev_ip
            dev_ip="$(ip -o -4 addr show dev "${ndev}" 2>/dev/null \
                | awk '{print $4}' | cut -d/ -f1 | head -1 || true)"
            if [[ -n "${dev_ip}" ]]; then
                echo "${dev_ip}"
                return
            fi
        fi
    fi
    # Fallback: first non-loopback, non-docker, non-virtual IP
    ip -o -4 addr show 2>/dev/null \
        | awk '{print $4}' \
        | cut -d/ -f1 \
        | grep -v '^127\.' \
        | grep -v '^172\.17\.' \
        | grep -v '^169\.254\.' \
        | head -1 || true
}

# ---- Select Python test script ----
TEST_SCRIPT="${SCRIPT_DIR}/test_rdma_minimal.py"
if [[ ! -f "${TEST_SCRIPT}" ]]; then
    TEST_SCRIPT="${SCRIPT_DIR}/test_mooncake_rdma.py"
fi

DEVICE_ARG=""
if [[ -n "${RDMA_DEVICE}" ]]; then
    DEVICE_ARG="--device ${RDMA_DEVICE}"
fi

case "${MODE}" in
    server)
        if [[ -z "${SERVER_RDMA_IP}" ]]; then
            SERVER_RDMA_IP="$(auto_detect_rdma_ip)"
        fi
        if [[ -z "${SERVER_RDMA_IP}" ]]; then
            echo "ERROR: Cannot auto-detect RDMA IP. Set SERVER_RDMA_IP manually."
            exit 1
        fi

        echo ""
        echo "============================================================"
        echo "  Mooncake RDMA Server - $(hostname)"
        echo "============================================================"
        echo " RDMA IP:   ${SERVER_RDMA_IP}"
        echo " Buf size:  ${BUF_SIZE}"
        echo " Device:    ${RDMA_DEVICE:-auto}"
        echo "============================================================"
        echo ""
        echo " >>> After startup, note the rpc_port from the output below."
        echo " >>> On the client node run:"
        echo " >>> SERVER_RDMA_IP=${SERVER_RDMA_IP} bash test_rdma.sh client <rpc_port>"
        echo ""

        python3 "${TEST_SCRIPT}" \
            --mode server \
            --local-ip "${SERVER_RDMA_IP}" \
            --size "${BUF_SIZE}" \
            ${DEVICE_ARG} \
            2>&1 | tee "${LOG_DIR}/rdma_server.log"
        ;;

    client)
        REMOTE_RPC_PORT="${2:-}"
        if [[ -z "${REMOTE_RPC_PORT}" ]]; then
            echo "ERROR: rpc_port is required."
            echo "Usage: bash test_rdma.sh client <rpc_port>"
            exit 1
        fi
        if [[ -z "${SERVER_RDMA_IP}" ]]; then
            echo "ERROR: SERVER_RDMA_IP must be set for client mode."
            echo "Usage: SERVER_RDMA_IP=<server_ip> bash test_rdma.sh client <rpc_port>"
            exit 1
        fi
        if [[ -z "${CLIENT_RDMA_IP}" ]]; then
            CLIENT_RDMA_IP="$(auto_detect_rdma_ip)"
        fi
        if [[ -z "${CLIENT_RDMA_IP}" ]]; then
            echo "ERROR: Cannot auto-detect client RDMA IP. Set CLIENT_RDMA_IP manually."
            exit 1
        fi

        echo ""
        echo "============================================================"
        echo "  Mooncake RDMA Client - $(hostname)"
        echo "============================================================"
        echo " Local RDMA IP:   ${CLIENT_RDMA_IP}"
        echo " Remote RDMA IP:  ${SERVER_RDMA_IP}"
        echo " Remote RPC Port: ${REMOTE_RPC_PORT}"
        echo " Buf size:        ${BUF_SIZE}"
        echo " Timeout:         ${TIMEOUT}s"
        echo " Device:          ${RDMA_DEVICE:-auto}"
        echo "============================================================"

        python3 "${TEST_SCRIPT}" \
            --mode client \
            --local-ip "${CLIENT_RDMA_IP}" \
            --remote-ip "${SERVER_RDMA_IP}" \
            --remote-rpc-port "${REMOTE_RPC_PORT}" \
            --size "${BUF_SIZE}" \
            --timeout "${TIMEOUT}" \
            ${DEVICE_ARG} \
            2>&1 | tee "${LOG_DIR}/rdma_client.log"
        ;;

    loopback)
        if [[ -n "${LOOPBACK_IP:-}" ]]; then
            LOCAL_RDMA_IP="${LOOPBACK_IP}"
        else
            LOCAL_RDMA_IP="$(auto_detect_rdma_ip)"
            if [[ -n "${LOCAL_RDMA_IP}" ]]; then
                echo "[auto-detect] Found local RDMA IP: ${LOCAL_RDMA_IP}"
            else
                echo "ERROR: Cannot auto-detect RDMA IP. Set LOOPBACK_IP manually."
                exit 1
            fi
        fi

        echo ""
        echo "============================================================"
        echo "  Mooncake RDMA Loopback Test - $(hostname)"
        echo "============================================================"
        echo " RDMA IP:  ${LOCAL_RDMA_IP}"
        echo " Buf size: ${BUF_SIZE}"
        echo " Device:   ${RDMA_DEVICE:-auto}"
        echo "============================================================"

        LOOPBACK_SCRIPT="${SCRIPT_DIR}/test_mooncake_rdma.py"
        if [[ ! -f "${LOOPBACK_SCRIPT}" ]]; then
            echo "ERROR: ${LOOPBACK_SCRIPT} not found, loopback mode requires test_mooncake_rdma.py"
            exit 1
        fi

        python3 "${LOOPBACK_SCRIPT}" \
            --mode loopback \
            --local-ip "${LOCAL_RDMA_IP}" \
            --size "${BUF_SIZE}" \
            ${DEVICE_ARG} \
            2>&1 | tee "${LOG_DIR}/rdma_loopback.log"
        ;;

    *)
        echo "ERROR: Unknown mode '${MODE}'. Use: server, client, or loopback"
        exit 1
        ;;
esac
