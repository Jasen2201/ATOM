# The gate's image on pit2-p03-g48: Ubuntu 24.04's /usr/bin/python3.12 runs the /share_nfs venvs, the apt toolchain
# builds atom/mesh, and the commands and system python3 packages that repo scripts and tests use are the dev host's.
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git libprotobuf-dev libssl-dev pkg-config protobuf-compiler \
        bc bsdextrautils file gawk iproute2 jq numactl psmisc rsync unzip \
        python3 python3-dev python3-venv \
        python3-jsonschema python3-packaging python3-requests python3-tqdm python3-yaml \
    && rm -rf /var/lib/apt/lists/*
