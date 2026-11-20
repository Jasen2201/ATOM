---
repo: Jasen2201/ATOM
integration_branch: jasen/atom-grpc
never_touch: ROCm/ATOM
gate_task: bash .claude/agent-team/gate.sh
gate_post_land: bash .claude/agent-team/post_land.sh
design_entry: docs/architecture_guide.md
new_tests_dir: tests
import_check: PYTHONPATH=. /share_nfs/yajizhan/venv-cpu-test/bin/python -c "import atom, os; print(os.path.dirname(os.path.abspath(atom.__file__)))"
publish_language: English
max_tasks: 3
test_files: tests/*,*/tests/*,test_*,*/test_*,*_test.*,*/tests.rs,*/test_support.rs,*/test_mocks/*
---

# ATOM gRPC line (jasen/atom-grpc)

Requirement: the umbrella issue #2. Tracking lives in wuhuikx/LLM-Booster#651
and its sub-issues; agents do not write there.

## Project rules

- Development and debugging stay in Jasen2201/ATOM. Nothing is pushed to
  ROCm/ATOM; the owner splits upstream PRs by hand once every feature works.
- CPU first. Every task is developed and accepted on CPU with fake or mock
  engines; no task needs a GPU. GPU end-to-end validation is a separate step
  the owner runs at the end. A bug found there is reproduced on the fake
  engine before it is fixed.
- Transport swap only: ATOM's existing P/D flow (relay and chunked transfer,
  `kv_transfer_params` as `AtomAdapter` handles it today) is kept exactly.
  The gRPC protocol is ATOM's own (`atom.grpc.v1`); do not adopt SGLang's or
  vLLM's proto or SGLang's bootstrap dual dispatch.
- Code comments and docstrings are at most two lines; the reasoning goes in
  the PR text.
- A PR stays within 1000 changed lines and 15 files.

## Environment (dev host)

- `gate_task` and `gate_post_land` run on pit2-p03-g48, in a container that
  mounts `/share_nfs` at its own path (`.claude/agent-team/g48/run.sh`), so
  every tree they gate, a detached one included, must be under `/share_nfs`,
  such as in `<worktree_root>`. `ATOM_GATE_LOCAL=1` runs them on the dev host.
  Their Rust toolchain and per-tree cargo targets persist in g48's docker volume
  `yajizhan-gate-rust`; remove a tree's two targets with the tree, here tree
  `22`'s:

  ```bash
  ssh pit2-p03-g48 docker run --rm -v yajizhan-gate-rust:/r ubuntu:24.04 rm -rf /r/target-22 /r/target-22-post
  ```
- Python tests: `/share_nfs/yajizhan/venv-cpu-test/bin/python -m pytest`
  (CPU torch, grpcio 1.78.0, protobuf 6.33, matching the ATOM image's
  `protobuf<7`). Set `TMPDIR` under `/dev/shm`: `/share_nfs` rejects `:` in
  file names, and `/` has little free space.
- Formatting: black 26.10.1 at `/share_nfs/yajizhan/venv-black26/bin/black`
  (CI's version); ruff at `/home/yajizhan@amd.com/.local/bin/ruff`. Whole-repo
  ruff is red on main, so the gate lints changed files only.
- Rust (atom/mesh): source `/dev/shm/yajizhan-rust/env.sh` (rustup, cargo,
  OpenSSL headers, protoc). `/dev/shm` is tmpfs; after a reboot the toolchain
  must be rebuilt (rustup into `/dev/shm/yajizhan-rust`, then
  `rustup component add rustfmt clippy`). The gate gives each tree its own
  `CARGO_TARGET_DIR` under `/dev/shm/yajizhan-rust`.
- Packages that importing `aiter` or `triton` need are absent here; tests that
  need them are skipped, as in CI's unit job.
- GPU node for the owner's final end-to-end run: pit2-p03-g48 (shared with
  another session; never stop containers you did not start).
