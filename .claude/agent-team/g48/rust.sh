#!/bin/bash
# Install the gate's Rust toolchain into g48's /dev/shm/yajizhan-rust (tmpfs) and write the env.sh the gate sources.
# run.sh runs it in the gate image before every run; 1.100.0 is the dev host's toolchain, so cargo fmt agrees.
set -euo pipefail
R=/dev/shm/yajizhan-rust
TOOLCHAIN=1.100.0
exec 9>"$R/.setup.lock"
flock 9
grep -qsxF "export RUSTUP_TOOLCHAIN=$TOOLCHAIN" "$R/env.sh" && exit 0
export RUSTUP_HOME=$R/rustup CARGO_HOME=$R/cargo
[ -x "$CARGO_HOME/bin/rustup" ] || curl -sSf https://sh.rustup.rs | sh -s -- -y -q --no-modify-path --default-toolchain none >/dev/null
"$CARGO_HOME/bin/rustup" toolchain install "$TOOLCHAIN" --profile minimal -c rustfmt -c clippy
cat >"$R/env.sh" <<EOF
export RUSTUP_HOME=$RUSTUP_HOME CARGO_HOME=$CARGO_HOME
export PATH=$CARGO_HOME/bin:\$PATH
export RUSTUP_TOOLCHAIN=$TOOLCHAIN
EOF
