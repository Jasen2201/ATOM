#!/usr/bin/env python3
"""
Mooncake RDMA multi-device bandwidth test.

Tests aggregate bandwidth across multiple ionic RDMA devices between two nodes.
Each device runs in its own process (not thread) to avoid GIL contention.

Usage:
  # Server (node A): listen on all 8 RDMA devices
  python test_rdma_bandwidth.py --mode server --num-devices 8 \
      --base-subnet <BASE_SUBNET> --local-suffix <LAST_OCTET>

  # Client (node B): write test
  python test_rdma_bandwidth.py --mode client --num-devices 8 \
      --base-subnet <BASE_SUBNET> --local-suffix <LAST_OCTET> --remote-suffix <SERVER_OCTET> \
      --server-ports <PORTS> --size 67108864 --iterations 20 --op write

  # Client (node B): read test
  python test_rdma_bandwidth.py --mode client --num-devices 8 \
      --base-subnet <BASE_SUBNET> --local-suffix <LAST_OCTET> --remote-suffix <SERVER_OCTET> \
      --server-ports <PORTS> --size 67108864 --iterations 20 --op read

Environment:
  MC_GID_INDEX=2   (IPv4-mapped GID for ionic)
"""

import argparse
import ctypes
import multiprocessing
import os
import sys
import time


def device_server(device_idx, local_ip, device_name, buf_size, result_queue, ready_event):
    """Run a server on a single RDMA device (in separate process)."""
    os.environ.setdefault("MC_GID_INDEX", "2")
    from mooncake.engine import TransferEngine

    tag = f"[srv-{device_idx}]"
    try:
        te = TransferEngine()
        ret = te.initialize(local_ip, "P2PHANDSHAKE", "rdma", device_name)
        if ret != 0:
            result_queue.put((device_idx, {"error": f"initialize failed: {ret}"}))
            ready_event.set()
            return

        rpc_port = te.get_rpc_port()
        buf = te.allocate_managed_buffer(buf_size)
        if buf == 0:
            result_queue.put((device_idx, {"error": "allocate_managed_buffer failed"}))
            ready_event.set()
            return

        # Fill with pattern
        pattern = bytes(range(256)) * (buf_size // 256 + 1)
        ctypes.memmove(buf, pattern[:buf_size], buf_size)

        result_queue.put((device_idx, {"rpc_port": rpc_port, "status": "ready"}))
        print(f"{tag} ready on {local_ip}:{rpc_port}, device={device_name}", flush=True)
        ready_event.set()

        # Keep running forever (parent will kill us)
        while True:
            time.sleep(1)

    except Exception as e:
        result_queue.put((device_idx, {"error": str(e)}))
        ready_event.set()


def device_client(device_idx, local_ip, remote_ip, remote_rpc_port,
                  device_name, buf_size, iterations, op, result_dict, barrier):
    """Run bandwidth test on a single RDMA device (in separate process)."""
    os.environ.setdefault("MC_GID_INDEX", "2")
    from mooncake.engine import TransferEngine

    tag = f"[cli-{device_idx}]"
    try:
        te = TransferEngine()
        ret = te.initialize(local_ip, "P2PHANDSHAKE", "rdma", device_name)
        if ret != 0:
            result_dict[device_idx] = {"error": f"initialize failed: {ret}"}
            barrier.wait()
            return

        remote_session = f"{remote_ip}:{remote_rpc_port}"

        buf = te.allocate_managed_buffer(buf_size)
        if buf == 0:
            result_dict[device_idx] = {"error": "allocate_managed_buffer failed"}
            barrier.wait()
            return

        # Fill with test data
        pattern = bytes(range(256)) * (buf_size // 256 + 1)
        ctypes.memmove(buf, pattern[:buf_size], buf_size)

        # Get remote buffer address
        remote_buf = te.get_first_buffer_address(remote_session)
        print(f"{tag} connected to {remote_session}, remote_buf={hex(remote_buf)}", flush=True)

        # Warmup
        for _ in range(3):
            if op == "write":
                te.transfer_sync_write(remote_session, buf, remote_buf, buf_size)
            else:
                te.transfer_sync_read(remote_session, buf, remote_buf, buf_size)

        # Synchronize all processes before benchmark
        barrier.wait()

        # Benchmark
        total_bytes = buf_size * iterations
        start = time.time()
        for _ in range(iterations):
            if op == "write":
                ret = te.transfer_sync_write(remote_session, buf, remote_buf, buf_size)
            else:
                ret = te.transfer_sync_read(remote_session, buf, remote_buf, buf_size)
            if ret != 0:
                print(f"{tag} {op} failed: {ret}", flush=True)
                break
        elapsed = time.time() - start
        bw_gbps = (total_bytes * 8) / elapsed / 1e9

        result_dict[device_idx] = {
            "bw_gbps": bw_gbps,
            "elapsed": elapsed,
            "iterations": iterations,
            "buf_size": buf_size,
            "op": op,
            "status": "ok",
        }
        print(f"{tag} {device_name} {op}={bw_gbps:.2f} Gbps ({elapsed:.3f}s)", flush=True)

    except Exception as e:
        result_dict[device_idx] = {"error": str(e)}
        try:
            barrier.wait()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Mooncake multi-device RDMA bandwidth test")
    parser.add_argument("--mode", required=True, choices=["server", "client"])
    parser.add_argument("--num-devices", type=int, default=8)
    parser.add_argument("--base-subnet", required=True,
                        help="Base subnet prefix, e.g. 192.168.100 -> ionic_0=192.168.100.x, ionic_1=192.168.101.x")
    parser.add_argument("--local-suffix", required=True, help="Last octet for local IPs")
    parser.add_argument("--remote-suffix", default="", help="Last octet for remote IPs (client)")
    parser.add_argument("--size", type=int, default=64*1024*1024, help="Transfer size (default 64MB)")
    parser.add_argument("--iterations", type=int, default=20, help="Iterations per device (default 20)")
    parser.add_argument("--server-ports", default="", help="Comma-separated RPC ports from server")
    parser.add_argument("--op", default="write", choices=["write", "read"],
                        help="Operation to benchmark (default write)")
    args = parser.parse_args()

    base_parts = args.base_subnet.split(".")
    base_third = int(base_parts[2])

    print(f"MC_GID_INDEX={os.environ.get('MC_GID_INDEX', 'NOT SET')}")
    print(f"Mode={args.mode}, Devices={args.num_devices}, Size={args.size/(1024*1024):.0f}MB, "
          f"Iters={args.iterations}, Op={args.op}")

    if args.mode == "server":
        result_queue = multiprocessing.Queue()
        events = []
        procs = []

        for i in range(args.num_devices):
            subnet_third = base_third + i
            local_ip = f"{base_parts[0]}.{base_parts[1]}.{subnet_third}.{args.local_suffix}"
            device_name = f"ionic_{i}"

            evt = multiprocessing.Event()
            events.append(evt)
            p = multiprocessing.Process(target=device_server,
                                        args=(i, local_ip, device_name, args.size, result_queue, evt),
                                        daemon=True)
            p.start()
            procs.append(p)

        for evt in events:
            evt.wait(timeout=30)

        # Collect results
        results = {}
        while not result_queue.empty():
            idx, data = result_queue.get_nowait()
            results[idx] = data

        ports = []
        print("\n" + "=" * 60)
        print("  All servers ready")
        print("=" * 60)
        for i in range(args.num_devices):
            r = results.get(i, {})
            if "error" in r:
                print(f"  ionic_{i}: ERROR - {r['error']}")
            else:
                port = r.get("rpc_port", 0)
                ports.append(str(port))
                print(f"  ionic_{i}: rpc_port={port}")

        ports_str = ",".join(ports)
        print(f"\n  --server-ports {ports_str}")
        print(f"\n  Waiting for client... (Ctrl+C to stop)")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        for p in procs:
            p.terminate()
        print("Server shut down.")

    elif args.mode == "client":
        if not args.remote_suffix:
            print("ERROR: --remote-suffix required"); sys.exit(1)
        if not args.server_ports:
            print("ERROR: --server-ports required"); sys.exit(1)

        server_ports = [int(p) for p in args.server_ports.split(",")]
        if len(server_ports) != args.num_devices:
            print(f"ERROR: expected {args.num_devices} ports, got {len(server_ports)}"); sys.exit(1)

        manager = multiprocessing.Manager()
        results = manager.dict()
        barrier = multiprocessing.Barrier(args.num_devices)
        procs = []

        for i in range(args.num_devices):
            subnet_third = base_third + i
            local_ip = f"{base_parts[0]}.{base_parts[1]}.{subnet_third}.{args.local_suffix}"
            remote_ip = f"{base_parts[0]}.{base_parts[1]}.{subnet_third}.{args.remote_suffix}"
            device_name = f"ionic_{i}"

            p = multiprocessing.Process(target=device_client,
                                        args=(i, local_ip, remote_ip, server_ports[i],
                                              device_name, args.size, args.iterations,
                                              args.op, results, barrier))
            p.start()
            procs.append(p)

        for p in procs:
            p.join(timeout=180)

        # Summary
        print("\n" + "=" * 70)
        print(f"  {args.op.upper()} Bandwidth ({args.num_devices} devices x "
              f"{args.size/(1024*1024):.0f} MB x {args.iterations} iters)")
        print("=" * 70)
        total_bw = 0
        ok_count = 0
        for i in range(args.num_devices):
            r = dict(results.get(i, {}))
            if r.get("status") == "ok":
                bw = r["bw_gbps"]
                total_bw += bw
                ok_count += 1
                print(f"  ionic_{i}: {bw:8.2f} Gbps  ({r['elapsed']:.3f}s)")
            else:
                print(f"  ionic_{i}: ERROR - {r.get('error', 'unknown')}")

        print("-" * 70)
        print(f"  Total ({ok_count} devices):   {total_bw:8.2f} Gbps")
        print(f"  Avg per device:       {total_bw/max(ok_count,1):8.2f} Gbps")
        print("=" * 70)


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    main()
