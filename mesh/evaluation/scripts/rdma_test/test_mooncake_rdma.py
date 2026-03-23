#!/usr/bin/env python3
"""
Mooncake Transfer Engine RDMA P2P handshake loopback + cross-node test.

This script tests the Mooncake Transfer Engine using the correct Python API:
  - TransferEngine.initialize(hostname, metadata_conn, protocol, device_name)
  - TransferEngine.get_rpc_port()
  - TransferEngine.allocate_managed_buffer(size)  OR  register_memory(addr, size)
  - TransferEngine.transfer_sync_write(target_session, src_addr, dst_addr, length)
  - TransferEngine.batch_transfer_sync_write(target_session, src_list, dst_list, len_list)

Environment variables:
  MC_GID_INDEX=2        - RoCE v2 GID index (2=IPv4-mapped, check ibv_devinfo for your device)
  GLOG_v=1              - Verbose C++ logging
  GLOG_logtostderr=1    - Log to stderr

Usage:
  # Single-node loopback test (simplest, tests engine basics):
  python test_mooncake_rdma.py --mode loopback --local-ip <LOCAL_RDMA_IP>

  # Two-process cross-node test:
  # On node A (server):
  python test_mooncake_rdma.py --mode server --local-ip <SERVER_RDMA_IP>
  # On node B (client):
  python test_mooncake_rdma.py --mode client --local-ip <CLIENT_RDMA_IP> --remote-ip <SERVER_RDMA_IP> --remote-rpc-port <PORT>

  # Run the pre-built C++ loopback test (no Python needed):
  /usr/local/bin/mooncake-tests/rdma_loopback_test -metadata_server P2PHANDSHAKE
"""

import argparse
import ctypes
import os
import sys
import time
import threading


def run_loopback_test(local_ip, protocol, buf_size, device_name=""):
    """Single-process loopback test using allocate_managed_buffer."""
    from mooncake.engine import TransferEngine

    print(f"[loopback] Initializing TransferEngine with hostname={local_ip}, protocol={protocol}, device={device_name!r}")
    te = TransferEngine()
    ret = te.initialize(local_ip, "P2PHANDSHAKE", protocol, device_name)
    if ret != 0:
        print(f"[loopback] ERROR: initialize() returned {ret}")
        sys.exit(1)

    rpc_port = te.get_rpc_port()
    session_id = f"{local_ip}:{rpc_port}"
    print(f"[loopback] Initialized OK. rpc_port={rpc_port}, session_id={session_id}")

    # Use allocate_managed_buffer (pre-registered memory)
    print(f"[loopback] Allocating {buf_size} bytes managed buffer...")
    src_buf = te.allocate_managed_buffer(buf_size)
    if src_buf == 0:
        print("[loopback] ERROR: allocate_managed_buffer returned 0 (failed)")
        # Fallback: try manual allocation + register_memory
        print("[loopback] Trying manual allocation with register_memory...")
        import numpy as np
        arr = np.zeros(buf_size * 2, dtype=np.uint8)
        src_buf = arr.ctypes.data
        ret = te.register_memory(src_buf, buf_size * 2)
        print(f"[loopback] register_memory returned {ret}")
        if ret != 0:
            print("[loopback] ERROR: register_memory failed")
            sys.exit(1)
        dst_buf = src_buf + buf_size
    else:
        print(f"[loopback] src_buf allocated at {hex(src_buf)}")
        dst_buf = te.allocate_managed_buffer(buf_size)
        if dst_buf == 0:
            print("[loopback] ERROR: second allocate_managed_buffer returned 0")
            sys.exit(1)
        print(f"[loopback] dst_buf allocated at {hex(dst_buf)}")

    # Fill source with test pattern
    test_pattern = bytes(range(256)) * (buf_size // 256 + 1)
    ctypes.memmove(src_buf, test_pattern[:buf_size], buf_size)
    # Clear destination
    ctypes.memset(dst_buf, 0, buf_size)

    # Verify source is filled
    src_bytes = (ctypes.c_ubyte * buf_size).from_address(src_buf)
    print(f"[loopback] Source first 16 bytes: {list(src_bytes[:16])}")

    # Attempt loopback write: write from src_buf to dst_buf on same engine
    print(f"[loopback] Attempting transfer_sync_write to self ({session_id})...")
    print(f"[loopback]   src_addr={hex(src_buf)}, dst_addr={hex(dst_buf)}, length={buf_size}")

    start = time.time()
    ret = te.transfer_sync_write(session_id, src_buf, dst_buf, buf_size)
    elapsed = time.time() - start
    print(f"[loopback] transfer_sync_write returned: {ret} (took {elapsed:.3f}s)")

    if ret == 0:
        # Verify data
        dst_bytes = (ctypes.c_ubyte * buf_size).from_address(dst_buf)
        errors = 0
        for i in range(min(buf_size, 256)):
            if dst_bytes[i] != i % 256:
                if errors < 5:
                    print(f"  Mismatch at offset {i}: expected {i % 256}, got {dst_bytes[i]}")
                errors += 1
        if errors == 0:
            bw_gbps = (buf_size * 8) / elapsed / 1e9 if elapsed > 0 else 0
            print(f"[loopback] PASS: Data verified ({buf_size} bytes, {bw_gbps:.2f} Gbps)")
        else:
            print(f"[loopback] FAIL: {errors} byte mismatches")
    else:
        print(f"[loopback] FAIL: transfer returned {ret}")

    # Also try batch API
    print(f"\n[loopback] Trying batch_transfer_sync_write...")
    ctypes.memset(dst_buf, 0, buf_size)
    ret = te.batch_transfer_sync_write(session_id, [src_buf], [dst_buf], [buf_size])
    print(f"[loopback] batch_transfer_sync_write returned: {ret}")
    if ret == 0:
        dst_bytes = (ctypes.c_ubyte * buf_size).from_address(dst_buf)
        ok = all(dst_bytes[i] == i % 256 for i in range(min(buf_size, 256)))
        print(f"[loopback] batch verify: {'PASS' if ok else 'FAIL'}")

    print("[loopback] Done.")


def run_server(local_ip, protocol, buf_size, device_name=""):
    """Server mode: initialize engine, register memory, print session info, wait."""
    from mooncake.engine import TransferEngine

    print(f"[server] Initializing TransferEngine with hostname={local_ip}, protocol={protocol}, device={device_name!r}")
    te = TransferEngine()
    ret = te.initialize(local_ip, "P2PHANDSHAKE", protocol, device_name)
    if ret != 0:
        print(f"[server] ERROR: initialize() returned {ret}")
        sys.exit(1)

    rpc_port = te.get_rpc_port()
    session_id = f"{local_ip}:{rpc_port}"
    print(f"[server] Initialized. session_id={session_id}")

    # Allocate and fill buffer
    buf = te.allocate_managed_buffer(buf_size)
    if buf == 0:
        import numpy as np
        arr = np.zeros(buf_size, dtype=np.uint8)
        buf = arr.ctypes.data
        te.register_memory(buf, buf_size)

    # Fill with test pattern
    test_pattern = bytes(range(256)) * (buf_size // 256 + 1)
    ctypes.memmove(buf, test_pattern[:buf_size], buf_size)
    print(f"[server] Buffer at {hex(buf)}, {buf_size} bytes filled with test pattern")
    print(f"[server] *** Client should use: --remote-rpc-port {rpc_port} ***")
    print(f"[server] Waiting for client transfers... (Ctrl+C to stop)")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    print("[server] Shutting down.")


def run_client(local_ip, remote_ip, remote_rpc_port, protocol, buf_size, device_name=""):
    """Client mode: initialize engine, write to remote server's buffer."""
    from mooncake.engine import TransferEngine

    remote_session = f"{remote_ip}:{remote_rpc_port}"
    print(f"[client] Initializing TransferEngine with hostname={local_ip}, protocol={protocol}, device={device_name!r}")
    te = TransferEngine()
    ret = te.initialize(local_ip, "P2PHANDSHAKE", protocol, device_name)
    if ret != 0:
        print(f"[client] ERROR: initialize() returned {ret}")
        sys.exit(1)

    rpc_port = te.get_rpc_port()
    print(f"[client] Initialized. local session={local_ip}:{rpc_port}")

    # Allocate local buffer
    buf = te.allocate_managed_buffer(buf_size)
    if buf == 0:
        import numpy as np
        arr = np.zeros(buf_size, dtype=np.uint8)
        buf = arr.ctypes.data
        te.register_memory(buf, buf_size)

    # Try to get remote buffer address
    print(f"[client] Looking up remote first buffer address for {remote_session}...")
    remote_buf = te.get_first_buffer_address(remote_session)
    print(f"[client] Remote buffer address: {hex(remote_buf)}")

    if remote_buf == 0:
        print("[client] WARNING: get_first_buffer_address returned 0, using 0 as dst")

    # Write our data to remote
    test_pattern = bytes(range(256)) * (buf_size // 256 + 1)
    ctypes.memmove(buf, test_pattern[:buf_size], buf_size)

    print(f"[client] Attempting transfer_sync_write to {remote_session}...")
    start = time.time()
    ret = te.transfer_sync_write(remote_session, buf, remote_buf, buf_size)
    elapsed = time.time() - start
    print(f"[client] transfer_sync_write returned: {ret} (took {elapsed:.3f}s)")

    if ret == 0:
        bw_gbps = (buf_size * 8) / elapsed / 1e9 if elapsed > 0 else 0
        print(f"[client] PASS: Write completed ({buf_size} bytes, {bw_gbps:.2f} Gbps)")
    else:
        print(f"[client] FAIL: transfer returned {ret}")

    # Also try read from remote
    print(f"\n[client] Attempting batch_transfer_sync_read from {remote_session}...")
    ctypes.memset(buf, 0, buf_size)
    ret = te.batch_transfer_sync_read(remote_session, [buf], [remote_buf], [buf_size])
    print(f"[client] batch_transfer_sync_read returned: {ret}")
    if ret == 0:
        read_bytes = (ctypes.c_ubyte * min(buf_size, 256)).from_address(buf)
        ok = all(read_bytes[i] == i % 256 for i in range(min(buf_size, 256)))
        print(f"[client] Read verify: {'PASS' if ok else 'FAIL'}")
        print(f"[client] First 16 bytes: {list(read_bytes[:16])}")

    print("[client] Done.")


def main():
    parser = argparse.ArgumentParser(
        description='Mooncake Transfer Engine RDMA P2P Test',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--mode', required=True, choices=['loopback', 'server', 'client'],
                        help='loopback=single-process self-test, server/client=two-process test')
    parser.add_argument('--local-ip', required=True, help='Local RDMA IP address')
    parser.add_argument('--remote-ip', default='', help='Remote RDMA IP (client mode only)')
    parser.add_argument('--remote-rpc-port', type=int, default=0,
                        help='Remote RPC port from server output (client mode only)')
    parser.add_argument('--protocol', default='rdma', choices=['rdma', 'tcp'])
    parser.add_argument('--size', type=int, default=4096, help='Transfer size in bytes (default 4096)')
    parser.add_argument('--device', default='', help='RDMA device filter (e.g. ionic_0). Empty=all')
    args = parser.parse_args()

    print(f"MC_GID_INDEX={os.environ.get('MC_GID_INDEX', 'NOT SET')}")
    print(f"GLOG_v={os.environ.get('GLOG_v', 'NOT SET')}")

    if args.mode == 'loopback':
        run_loopback_test(args.local_ip, args.protocol, args.size, args.device)
    elif args.mode == 'server':
        run_server(args.local_ip, args.protocol, args.size, args.device)
    elif args.mode == 'client':
        if not args.remote_ip or not args.remote_rpc_port:
            print("ERROR: --remote-ip and --remote-rpc-port required for client mode")
            sys.exit(1)
        run_client(args.local_ip, args.remote_ip, args.remote_rpc_port, args.protocol, args.size, args.device)


if __name__ == '__main__':
    main()
