# The gate's image on pit2-p03-g48: Ubuntu 24.04's /usr/bin/python3.12 runs the /share_nfs venvs,
# and the apt toolchain builds atom/mesh; run.sh tags it by this file's hash and builds it on first use.
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git libprotobuf-dev libssl-dev pkg-config protobuf-compiler \
        python3 python3-dev python3-venv \
    && rm -rf /var/lib/apt/lists/*
