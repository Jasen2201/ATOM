#!/usr/bin/env python3
"""
Minimal RDMA cross-node test - no TCP probing, clean handshake.
Uses single NIC (rdma0), verbose logging, SIGALRM timeout.
"""
import argparse
import ctypes
import os
import sys
import time
import signal


def timeout_handler(signum, frame):
    import traceback
    print(f"\n[TIMEOUT] Hit {signum}", flush=True)
    for tid, stack in sys._current_frames().items():
        print(f"\n--- Thread {tid} ---", flush=True)
        traceback.print_stack(stack)
    os._exit(1)


def run_server(local_ip, device_name, buf_size):
    from mooncake.engine import TransferEngine

    print(f"[server] init: hostname={local_ip}, device={device_name!r}", flush=True)
    te = TransferEngine()
    ret = te.initialize(local_ip, "P2PHANDSHAKE", "rdma", device_name)
    assert ret == 0, f"initialize failed: {ret}"

    rpc_port = te.get_rpc_port()
    print(f"[server] rpc_port={rpc_port}", flush=True)

    buf = te.allocate_managed_buffer(buf_size)
    assert buf != 0, "allocate_managed_buffer failed"
    pattern = bytes(range(256)) * (buf_size // 256 + 1)
    ctypes.memmove(buf, pattern[:buf_size], buf_size)
    print(f"[server] buf={hex(buf)}, size={buf_size}", flush=True)
    print(f"[server] remote-rpc-port {rpc_port}", flush=True)
    print(f"[server] waiting...", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def run_client(local_ip, remote_ip, remote_rpc_port, device_name, buf_size, timeout_sec):
    from mooncake.engine import TransferEngine

    remote_session = f"{remote_ip}:{remote_rpc_port}"
    print(f"[client] init: hostname={local_ip}, device={device_name!r}", flush=True)
    te = TransferEngine()
    ret = te.initialize(local_ip, "P2PHANDSHAKE", "rdma", device_name)
    assert ret == 0, f"initialize failed: {ret}"

    rpc_port = te.get_rpc_port()
    print(f"[client] local rpc_port={rpc_port}", flush=True)

    # Allocate local buffer FIRST (before metadata exchange, to avoid the pool alloc interfering)
    buf = te.allocate_managed_buffer(buf_size)
    assert buf != 0, "allocate_managed_buffer failed"
    pattern = bytes(range(256)) * (buf_size // 256 + 1)
    ctypes.memmove(buf, pattern[:buf_size], buf_size)
    print(f"[client] local buf={hex(buf)}", flush=True)

    # Get remote buffer address
    print(f"[client] get_first_buffer_address({remote_session})...", flush=True)
    remote_buf = te.get_first_buffer_address(remote_session)
    print(f"[client] remote_buf={hex(remote_buf)}", flush=True)
    assert remote_buf != 0, "remote buffer is 0"

    # Set timeout
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(timeout_sec)

    # Transfer
    print(f"[client] transfer_sync_write -> {remote_session}", flush=True)
    print(f"[client]   src={hex(buf)} dst={hex(remote_buf)} len={buf_size}", flush=True)
    t0 = time.time()
    ret = te.transfer_sync_write(remote_session, buf, remote_buf, buf_size)
    dt = time.time() - t0
    signal.alarm(0)

    if ret == 0:
        bw = (buf_size * 8) / dt / 1e9 if dt > 0 else 0
        print(f"[client] PASS: ret=0, {dt:.3f}s, {bw:.2f} Gbps", flush=True)
    else:
        print(f"[client] FAIL: ret={ret}, {dt:.3f}s", flush=True)
        sys.exit(1)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--mode', required=True, choices=['server', 'client'])
    p.add_argument('--local-ip', required=True)
    p.add_argument('--remote-ip', default='')
    p.add_argument('--remote-rpc-port', type=int, default=0)
    p.add_argument('--device', default='', help='RDMA device (e.g. rdma0). Empty=all')
    p.add_argument('--size', type=int, default=4096)
    p.add_argument('--timeout', type=int, default=30)
    a = p.parse_args()

    print(f"MC_GID_INDEX={os.environ.get('MC_GID_INDEX', 'UNSET')}", flush=True)

    if a.mode == 'server':
        run_server(a.local_ip, a.device, a.size)
    else:
        assert a.remote_ip and a.remote_rpc_port, "need --remote-ip and --remote-rpc-port"
        run_client(a.local_ip, a.remote_ip, a.remote_rpc_port, a.device, a.size, a.timeout)


if __name__ == '__main__':
    main()
