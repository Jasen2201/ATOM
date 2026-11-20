#!/bin/bash
# agent-team gate_targeted for jasen/atom-grpc: gate.sh's lint, then only those of gate.sh's tests the change reaches.
# Run from a tree root; exit 0 is a pass. It runs on pit2-p03-g48 through g48/run.sh unless ATOM_GATE_LOCAL=1.
set -uo pipefail
[ "${ATOM_GATE_LOCAL:-}" = 1 ] || exec bash "$(dirname "$0")/g48/run.sh" "$0"

PY=/share_nfs/yajizhan/venv-cpu-test/bin/python
BLACK=/share_nfs/yajizhan/venv-black26/bin/black
RUFF=/home/yajizhan@amd.com/.local/bin/ruff
RUST_ENV=/dev/shm/yajizhan-rust/env.sh
SELECT=$(dirname "$0")/select_tests.py

TREE=$(pwd)
NAME=$(basename "$TREE")
SCR=/dev/shm/yajizhan-gate/$NAME-targeted
TARGET=/dev/shm/yajizhan-rust/target-$NAME  # this tree's cargo target directory, as in gate.sh
mkdir -p "$SCR"

FORK=$(git remote -v | awk '$2 ~ /Jasen2201\/ATOM(\.git)?$/ && $3 == "(fetch)" {print $1; exit}')
[ -n "$FORK" ] || { echo "gate: no remote for Jasen2201/ATOM"; exit 2; }
BASE=$(git merge-base HEAD "$FORK/jasen/atom-grpc") || { echo "gate: no merge base with $FORK/jasen/atom-grpc"; exit 2; }
UPSTREAM=$(git remote -v | awk 'tolower($2) ~ /[:\/]rocm\/atom(\.git)?$/ && $3 == "(fetch)" {print $1; exit}')
[ -n "$UPSTREAM" ] || { echo "gate: no remote for ROCm/ATOM"; exit 2; }
UBASE=$(git merge-base HEAD "$UPSTREAM/main") || { echo "gate: no merge base with $UPSTREAM/main"; exit 2; }

changed() {  # files changed since commit $1 (committed or not) plus untracked ones, filtered by the pathspecs after it
    local since=$1
    shift
    { git diff --name-only --diff-filter=ACMR "$since" -- "$@"; git ls-files --others --exclude-standard -- "$@"; } | sort -u
}

fail=0
# gate.sh's lint: Python files that differ from both the line's base and upstream main.
mapfile -t PYFILES < <(comm -12 <(changed "$BASE" '*.py') <(changed "$UBASE" '*.py'))
if ((${#PYFILES[@]})); then
    echo "== ruff (${#PYFILES[@]} changed files)"
    "$RUFF" check "${PYFILES[@]}" || fail=1
    echo "== black 26"
    "$BLACK" --check "${PYFILES[@]}" || fail=1
fi

# gate.sh's pytest arguments, of whose test files select_tests.py keeps those the change reaches.
mapfile -t TESTS < <(printf '%s\n' "${PYFILES[@]}" | grep -E '^tests/(.*/)?test_[^/]*\.py$' | grep -v -e '^tests/entrypoints/' -e '^tests/atom_grpc/')
[ -d tests/atom_grpc ] && TESTS+=(tests/atom_grpc)
"$PY" "$SELECT" pytest "$BASE" tests/entrypoints "${TESTS[@]}" >"$SCR/pytest.txt" || { echo "gate: select_tests.py failed"; exit 2; }
mapfile -t PICKED <"$SCR/pytest.txt"
# Tests under tests/atom_grpc start atomesh: build it first, as CI's unit-test job does.
unset ATOMESH_BIN
if [[ " ${PICKED[*]} " == *" tests/atom_grpc/"* ]]; then
    [ -f "$RUST_ENV" ] || { echo "gate: $RUST_ENV missing (see the overlay body)"; exit 2; }
    echo "== atomesh: cargo build --profile ci --bin atomesh"
    if (. "$RUST_ENV" && cd atom/mesh && CARGO_TARGET_DIR=$TARGET cargo build --profile ci --bin atomesh -q -j 16); then
        export ATOMESH_BIN=$TARGET/ci/atomesh
    else
        fail=1
    fi
fi
if ((${#PICKED[@]})); then
    echo "== pytest ${PICKED[*]}"
    TMPDIR=$SCR PYTHONPATH=. "$PY" -m pytest -q -p no:cacheprovider --basetemp="$SCR/bt" "${PICKED[@]}"
    rc=$?
    [ $rc = 0 ] || [ $rc = 5 ] || fail=1  # 5: the files picked hold no test, which gate.sh runs among others
else
    echo "== pytest: the change reaches none of gate.sh's test files"
fi

# mesh: when gate.sh runs it, cargo fmt, then the library and every integration target, or only the targets reached.
if [ -n "$(changed "$BASE" 'atom/mesh/*' '*.proto')" ]; then
    [ -f "$RUST_ENV" ] || { echo "gate: $RUST_ENV missing (see the overlay body)"; exit 2; }
    ITESTS=()
    for f in atom/mesh/tests/*.rs; do
        n=$(basename "$f" .rs)
        case "$n" in
            ext_proc_tests | ext_proc_envoy) ;;  # need the ext-proc feature
            *) ITESTS+=("$n") ;;
        esac
    done
    "$PY" "$SELECT" mesh "$BASE" "${ITESTS[@]}" >"$SCR/mesh.txt" || { echo "gate: select_tests.py failed"; exit 2; }
    mapfile -t MESH <"$SCR/mesh.txt"
    echo "== mesh: cargo fmt --check, then ${MESH[*]:-no test target}"
    (
        . "$RUST_ENV"
        export CARGO_TARGET_DIR=$TARGET
        cd atom/mesh
        cargo fmt --check || exit 1
        if [ "${MESH[*]}" = all ]; then
            T=()
            for n in "${ITESTS[@]}"; do T+=(--test "$n"); done
            # gate.sh's commands; integration tests one test at a time until #7, as there.
            cargo check --all-targets -q -j 16 \
                && cargo test --features atom-engine --lib -q -j 16 \
                && cargo test --features atom-engine "${T[@]}" -q -j 16 -- --test-threads=1
        elif ((${#MESH[@]})); then
            T=()
            for n in "${MESH[@]}"; do T+=(--test "$n"); done
            cargo check "${T[@]}" -q -j 16 \
                && cargo test --features atom-engine "${T[@]}" -q -j 16 -- --test-threads=1
        fi
    ) || fail=1
fi

echo "gate: $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
