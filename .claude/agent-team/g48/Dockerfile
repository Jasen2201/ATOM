# The gate's image on pit2-p03-g48: Ubuntu 24.04's /usr/bin/python3.12 runs the /share_nfs venvs,
# the apt toolchain builds atom/mesh, and system python3 has the dev host's packages that repo scripts import.
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git libprotobuf-dev libssl-dev pkg-config protobuf-compiler \
        python3 python3-dev python3-venv \
        python3-jsonschema python3-packaging python3-requests python3-tqdm python3-yaml \
    && rm -rf /var/lib/apt/lists/*
