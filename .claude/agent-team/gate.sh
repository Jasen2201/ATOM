#!/bin/bash
# agent-team gate_task for jasen/atom-grpc: lint + CPU tests, no GPU.
# Run from a tree root; exit 0 is a pass.
set -uo pipefail

PY=/share_nfs/yajizhan/venv-cpu-test/bin/python
BLACK=/share_nfs/yajizhan/venv-black26/bin/black
RUFF=/home/yajizhan@amd.com/.local/bin/ruff
RUST_ENV=/dev/shm/yajizhan-rust/env.sh

TREE=$(pwd)
NAME=$(basename "$TREE")
SCR=/dev/shm/yajizhan-gate/$NAME
mkdir -p "$SCR"

FORK=$(git remote -v | awk '$2 ~ /Jasen2201\/ATOM(\.git)?$/ && $3 == "(fetch)" {print $1; exit}')
[ -n "$FORK" ] || { echo "gate: no remote for Jasen2201/ATOM"; exit 2; }
BASE=$(git merge-base HEAD "$FORK/jasen/atom-grpc") || { echo "gate: no merge base with $FORK/jasen/atom-grpc"; exit 2; }

changed() {  # files changed since BASE (committed or not) plus untracked ones, filtered by pathspec
    { git diff --name-only --diff-filter=ACMR "$BASE" -- "$@"; git ls-files --others --exclude-standard -- "$@"; } | sort -u
}

fail=0
mapfile -t PYFILES < <(changed '*.py')
if ((${#PYFILES[@]})); then
    echo "== ruff (${#PYFILES[@]} changed files)"
    "$RUFF" check "${PYFILES[@]}" || fail=1
    echo "== black 26"
    "$BLACK" --check "${PYFILES[@]}" || fail=1
fi

# tests/entrypoints always, plus every changed or new test file elsewhere under tests/.
mapfile -t TESTS < <(printf '%s\n' "${PYFILES[@]}" | grep -E '^tests/(.*/)?test_[^/]*\.py$' | grep -v '^tests/entrypoints/')
DESELECT=(
    # Fail on main 52a0cf790 in this venv as well; not caused by this line of work.
    --deselect "tests/entrypoints/test_structured_output.py::test_relaxed_mtp_rejected_on_execution_worker"
    --deselect "tests/entrypoints/test_structured_output.py::test_strict_arguments_are_rechecked_per_keyword[schema0-12345-1234]"
    --deselect "tests/entrypoints/test_structured_output.py::test_strict_schemas_close_every_object[city5-patternProperties]"
)
echo "== pytest tests/entrypoints ${TESTS[*]}"
TMPDIR=$SCR PYTHONPATH=. "$PY" -m pytest -q -p no:cacheprovider --basetemp="$SCR/bt" \
    "${DESELECT[@]}" tests/entrypoints "${TESTS[@]}" || fail=1

# mesh: only when the change touches atom/mesh or a .proto file.
if [ -n "$(changed 'atom/mesh/*' '*.proto')" ]; then
    [ -f "$RUST_ENV" ] || { echo "gate: $RUST_ENV missing (see the overlay body)"; exit 2; }
    echo "== mesh: cargo fmt --check, cargo test --lib"
    (
        . "$RUST_ENV"
        export CARGO_TARGET_DIR=/dev/shm/yajizhan-rust/target-$NAME
        cd atom/mesh && cargo fmt --check && cargo test --lib -q -j 16
    ) || fail=1
fi

echo "gate: $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
