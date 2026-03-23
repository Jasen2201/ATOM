# Mooncake RDMA Connectivity & Bandwidth Test

RDMA connectivity and bandwidth testing tools based on the Mooncake TransferEngine. Supports single-node loopback, cross-node server/client connectivity tests, and multi-device parallel bandwidth benchmarks.

## Files

| File | Description |
|------|-------------|
| `test_rdma.sh` | Entry script wrapping three test modes (loopback / server / client) |
| `test_mooncake_rdma.py` | Full connectivity test with loopback / server / client modes |
| `test_rdma_minimal.py` | Minimal cross-node server/client test with timeout support |
| `test_rdma_bandwidth.py` | Multi-device parallel bandwidth benchmark (multiprocessing) |

## Prerequisites

1. **Run inside a Docker container** with the following flags:
   ```bash
   docker run -d \
     --cap-add=IPC_LOCK \
     --cap-add=NET_RAW \
     --network=host \
     --device=/dev/infiniband \
     --ulimit memlock=-1:-1 \
     --shm-size 60g \
     -v /mnt:/mnt \
     ...
   ```

2. **RDMA devices visible**: `ibv_devinfo` inside the container should show available devices.
   - For Pensando ionic NICs, the Docker image must include the provider plugin and driver config (see [Pensando ionic Setup](#pensando-ionic-setup) below).

3. **Mooncake installed**: `python3 -c "from mooncake.engine import TransferEngine"` should succeed.

4. **IPv4 configured on RDMA interfaces**: Mooncake requires IPv4 for RPC handshake and GID matching.

## Usage

### 1. Single-Node Loopback Test

Verify that the local RDMA device and Mooncake engine work correctly:

```bash
# Auto-detect IP
bash test_rdma.sh loopback

# Specify IP manually
LOOPBACK_IP=<YOUR_RDMA_IP> bash test_rdma.sh loopback

# Pin to a specific RDMA device
LOOPBACK_IP=<YOUR_RDMA_IP> RDMA_DEVICE=ionic_0 bash test_rdma.sh loopback
```

### 2. Cross-Node Connectivity Test

Run server and client in Docker containers on two separate machines:

```bash
# --- Node A (server) ---
SERVER_RDMA_IP=<SERVER_IP> RDMA_DEVICE=<DEVICE> bash test_rdma.sh server
# Note the rpc_port from the output, e.g.: [server] rpc_port=15740

# --- Node B (client) ---
SERVER_RDMA_IP=<SERVER_IP> CLIENT_RDMA_IP=<CLIENT_IP> RDMA_DEVICE=<DEVICE> \
  bash test_rdma.sh client <rpc_port>
```

### 3. Multi-Device Bandwidth Benchmark

Test aggregate bandwidth across all 8 ionic devices between two nodes. Each device runs in its own process to avoid Python GIL contention.

**IP addressing**: Each RDMA device should use a separate subnet for traffic isolation. For example, with `--base-subnet 10.0.100`:
- device 0 uses `10.0.100.x`, device 1 uses `10.0.101.x`, device 2 uses `10.0.102.x`, etc.

**Step 1 — Find the network interface name for each RDMA device**:
```bash
# List all RDMA devices and their associated network interfaces
for dev in $(ls /sys/class/infiniband/); do
  ndev=$(cat /sys/class/infiniband/$dev/ports/1/gid_attrs/ndevs/0 2>/dev/null)
  echo "$dev -> $ndev"
done
```

**Step 2 — Assign an IPv4 address to each interface** (on the host, not inside the container):
```bash
# Pick a base subnet (e.g. 10.0.100) and a suffix unique to this node (e.g. 1).
# Each device gets subnet = base_third + device_index.
BASE=10.0.100    # choose any unused private range
SUFFIX=1         # unique per node (e.g. node A=1, node B=2)

# Assign IPs — replace <ifN> with actual interface names from Step 1
sudo ip addr add ${BASE}.${SUFFIX}/24       dev <if0>   # device 0
sudo ip addr add 10.0.101.${SUFFIX}/24      dev <if1>   # device 1
sudo ip addr add 10.0.102.${SUFFIX}/24      dev <if2>   # device 2
# ... repeat for all devices
```

> Tip: automate with a loop:
> ```bash
> IFACES=(<if0> <if1> <if2> <if3> <if4> <if5> <if6> <if7>)
> for i in "${!IFACES[@]}"; do
>   sudo ip addr add 10.0.$((100+i)).${SUFFIX}/24 dev "${IFACES[$i]}"
> done
> ```

> Note: `ip addr add` is ephemeral and lost on reboot. For persistence, use netplan or network-scripts.

**Step 3 — Run the benchmark**:
```bash
# --- Node A (server) ---
MC_GID_INDEX=2 python3 test_rdma_bandwidth.py \
  --mode server --num-devices 8 \
  --base-subnet 10.0.100 --local-suffix 1 \
  --size 67108864
# Note the --server-ports output

# --- Node B (client, write test) ---
MC_GID_INDEX=2 python3 test_rdma_bandwidth.py \
  --mode client --num-devices 8 \
  --base-subnet 10.0.100 --local-suffix 2 --remote-suffix 1 \
  --server-ports <ports_from_server> \
  --size 67108864 --iterations 20 --op write

# --- Node B (client, read test) ---
MC_GID_INDEX=2 python3 test_rdma_bandwidth.py \
  --mode client --num-devices 8 \
  --base-subnet 10.0.100 --local-suffix 2 --remote-suffix 1 \
  --server-ports <ports_from_server> \
  --size 67108864 --iterations 20 --op read
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVER_RDMA_IP` | auto-detect | Server-side RDMA interface IPv4 |
| `CLIENT_RDMA_IP` | auto-detect | Client-side RDMA interface IPv4 |
| `LOOPBACK_IP` | auto-detect | IPv4 used in loopback mode |
| `RDMA_DEVICE` | empty (all) | Pin to a specific RDMA device, e.g. `ionic_0` |
| `MC_GID_INDEX` | `2` | RoCE v2 GID index (2 = IPv4-mapped; verify with `ibv_devinfo`) |
| `BUF_SIZE` | `4096` | Transfer buffer size in bytes (connectivity tests) |
| `TIMEOUT` | `30` | Client-side transfer timeout in seconds |

## Pensando ionic Setup

Pensando DSC-200 (AMD ionic) SmartNICs require additional setup for RDMA inside containers.

### 1. Provider Plugin and Driver Config

The Docker image built with `Dockerfile_mesh` automatically:
- Copies all host RDMA provider plugins (including `libionic-rdmav34.so`)
- Generates `/etc/libibverbs.d/*.driver` config files

If building manually, ensure these are present inside the container:
```bash
# Provider plugin
/usr/lib/x86_64-linux-gnu/libibverbs/libionic-rdmav34.so

# Driver config
/etc/libibverbs.d/ionic.driver  # contents: "driver ionic"
```

Verify with:
```bash
ibv_devinfo | head -5
```

### 2. Configure IPv4 on RDMA Interfaces

ionic ports may not have IPv4 addresses by default. Configure them on the **host** (not inside the container, since `--network=host` shares the network namespace).

First, find which network interface maps to each RDMA device:
```bash
cat /sys/class/infiniband/ionic_0/ports/1/gid_attrs/ndevs/0
# e.g. output: enp9s0
```

Then assign an IPv4 address:
```bash
sudo ip addr add <IP>/<MASK> dev <interface>
```

> Note: `ip addr add` is ephemeral and lost on reboot. For persistence, use netplan or network-scripts.

### 3. Firewall Rules

Allow RDMA traffic through the firewall on both nodes:
```bash
sudo ufw allow from <RDMA_SUBNET>
# For multi-device tests with multiple subnets:
for s in $(seq <START> <END>); do sudo ufw allow from <PREFIX>.$s.0/24; done
```

### 4. Mooncake Buffer Size Limit

ionic has `max_mr_size = 2GB`, while upstream Mooncake defaults to `kDefaultBufferCapacity = 2GB`. This edge case causes `ibv_reg_mr` to fail with ENOMEM.

The MESH Docker image uses a [patched Mooncake fork](https://github.com/Jasen2201/Mooncake) with `kDefaultBufferCapacity = 1GB`.

## Example Output

### Loopback (pass)

```
[loopback] Initialized OK. rpc_port=16056, session_id=<IP>:16056
[loopback] transfer_sync_write returned: 0 (took 0.023s)
[loopback] PASS: Data verified (4096 bytes)
[loopback] batch verify: PASS
```

### Cross-Node (pass)

```
[client] transfer_sync_write returned: 0 (took 0.135s)
[client] PASS: Write completed (4096 bytes)
[client] Read verify: PASS
```

### Bandwidth Benchmark (8 x ionic DSC-200, 400 Gbps/port)

```
======================================================================
  WRITE Bandwidth (8 devices x 64 MB x 20 iters)
======================================================================
  ionic_0:   207.39 Gbps  (0.052s)
  ionic_1:   293.87 Gbps  (0.037s)
  ionic_2:   305.38 Gbps  (0.035s)
  ionic_3:   208.87 Gbps  (0.051s)
  ionic_4:   213.66 Gbps  (0.050s)
  ionic_5:   212.80 Gbps  (0.050s)
  ionic_6:   299.51 Gbps  (0.036s)
  ionic_7:   335.58 Gbps  (0.032s)
----------------------------------------------------------------------
  Total (8 devices):    2077.05 Gbps
  Avg per device:         259.63 Gbps
======================================================================

======================================================================
  READ Bandwidth (8 devices x 64 MB x 20 iters)
======================================================================
  ionic_0:   201.71 Gbps  (0.053s)
  ionic_1:   281.88 Gbps  (0.038s)
  ionic_2:   202.81 Gbps  (0.053s)
  ionic_3:   206.61 Gbps  (0.052s)
  ionic_4:   196.23 Gbps  (0.055s)
  ionic_5:   201.43 Gbps  (0.053s)
  ionic_6:   203.04 Gbps  (0.053s)
  ionic_7:   285.88 Gbps  (0.038s)
----------------------------------------------------------------------
  Total (8 devices):    1779.59 Gbps
  Avg per device:         222.45 Gbps
======================================================================
```

### Comparison: Mooncake vs Raw perftest

| | Raw (ib_write_bw / ib_read_bw) | Mooncake | Efficiency |
|---|---|---|---|
| Write per-port avg | 327 Gbps | 260 Gbps | 79% |
| Write 8-port total | 2615 Gbps | 2077 Gbps | 79% |
| Read per-port avg | 261 Gbps | 222 Gbps | 85% |
| Read 8-port total | 2091 Gbps | 1780 Gbps | 85% |

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `No RDMA devices found` | Missing ionic provider plugin or `.driver` config | Rebuild image with `Dockerfile_mesh` or manually install provider |
| `Failed to register memory: Cannot allocate memory` | ionic `max_mr_size=2GB` limit | Use patched Mooncake with `kDefaultBufferCapacity=1GB` |
| `fork compatibility: Invalid argument` | ionic does not support `ibv_fork_init` | Harmless warning, can be ignored |
| Endpoint handshake timeout | Multiple ionic ports but only some have IPv4 | Pin to a specific device: `RDMA_DEVICE=ionic_0` |
| `Connection timed out` on RPC | Firewall blocking RDMA subnet | `sudo ufw allow from <RDMA_SUBNET>` |
| GID is NULL | Wrong `MC_GID_INDEX` | Check GID table: `cat /sys/class/infiniband/ionic_0/ports/1/gids/*` |
| Read bandwidth near zero | Python GIL contention (threading) | Use `test_rdma_bandwidth.py` which uses multiprocessing |
