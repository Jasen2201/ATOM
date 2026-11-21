#!/bin/bash
# `bash tests/atom_grpc/regression.sh`: CI's CPU test scope and atomesh's cargo tests at HEAD and at its merge base
# with ROCm/ATOM main; prints the failures HEAD adds, each rerun once at either commit, and the tests HEAD edits.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
PY=${ATOM_ACCEPT_PYTHON:-/share_nfs/yajizhan/venv-cpu-test/bin/python}
RUST_ENV=${ATOM_ACCEPT_RUST_ENV:-/dev/shm/yajizhan-rust/env.sh}
[ ! -f "$RUST_ENV" ] || . "$RUST_ENV"
cd "$ROOT"
if [ -z "${REGRESSION_BASE:-}" ]; then
    UP=$(git remote -v | awk 'tolower($2) ~ /[:\/]rocm\/atom(\.git)?$/ && $3 == "(fetch)" {print $1; exit}')
    [ -n "$UP" ] || { echo "regression.sh: no remote for ROCm/ATOM" >&2; exit 2; }
    REGRESSION_BASE=$(git merge-base HEAD "$UP/main")
fi
BASE=$(git rev-parse "$REGRESSION_BASE")
# The base runs in a detached worktree beside this one, kept for the next run.
BTREE=${REGRESSION_BASE_TREE:-$(dirname "$ROOT")/regression-base-${BASE:0:12}}
[ -d "$BTREE" ] || git worktree add -q --detach "$BTREE" "$BASE"
[ "$(git -C "$BTREE" rev-parse HEAD)" = "$BASE" ] || { echo "regression.sh: $BTREE is not at $BASE" >&2; exit 2; }
OUT=$(mktemp -d "${REGRESSION_OUT:-/dev/shm}/regression.XXXXXX")
export REGRESSION_TIP=$ROOT REGRESSION_BASE_TREE=$BTREE REGRESSION_BASE=$BASE REGRESSION_OUT=$OUT
export REGRESSION_TARGETS=${CARGO_TARGET_DIR:-/dev/shm/yajizhan-rust/target}  # one cargo target per tree, beside it

read -r -d '' DRIVER <<'EOF' || true
import os, re, subprocess, sys
from pathlib import Path
from xml.etree import ElementTree as ET

TIP, BTREE, BASE, OUT = (os.environ[f"REGRESSION_{k}"] for k in ("TIP", "BASE_TREE", "BASE", "OUT"))
IGNORE = ["tests/plugin", "tests/entrypoints/test_openai_server.py", "tests/test_atomesh_mooncake_store.py"]
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors"]
PYTEST += ["--rootdir=.", "-o", "junit_family=xunit1"]


def env(tree, name):
    tmp = Path(OUT, name)
    tmp.mkdir(exist_ok=True)
    target = f"{os.environ['REGRESSION_TARGETS']}-{Path(tree).name}"
    return os.environ | {"PYTHONPATH": tree, "TMPDIR": str(tmp), "CARGO_TARGET_DIR": target}


def pytest_failures(tree, name, args):
    """Node ids whose test failed or errored, collection errors as their file's id."""
    report = Path(OUT, f"{name}.xml")
    bt = f"--basetemp={OUT}/{name}/bt"  # short: the fake instances' IPC socket paths fit 107 bytes
    subprocess.run([*PYTEST, bt, f"--junitxml={report}", *args], cwd=tree, env=env(tree, name), stdout=subprocess.DEVNULL)
    failed = []
    for case in ET.parse(report).iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        module, cls, test = case.get("file"), case.get("classname"), case.get("name")
        if not module:  # a collection error names its module as a dotted path
            failed.append(test.replace(".", "/") + ".py")
            continue
        inner = cls[len(module[:-3].replace("/", ".")) + 1 :].replace(".", "::")
        failed.append("::".join(p for p in (module, inner, test) if p))
    return failed


def cargo_failures(tree, name, args):
    """(target args, test) of each failed cargo test; ("build", "") when nothing ran."""
    mesh = Path(tree, "atom/mesh")
    if not mesh.is_dir():
        return []
    argv = ["cargo", "test", "--features", "atom-engine", *args]  # cargo names each test binary on stderr
    run = subprocess.run(argv, cwd=mesh, env=env(tree, name), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    Path(OUT, f"{name}.log").write_text(run.stdout)
    failed, target = [], None
    for line in run.stdout.splitlines():
        if m := re.match(r"\s*Running (unittests src/lib\.rs|tests/(\S+)\.rs|unittests )", line):
            target = ("--lib",) if m[1].endswith("lib.rs") else ("--test", m[2]) if m[2] else ("--bins",)
        elif m := re.match(r"test (\S+) \.\.\. FAILED$", line):
            failed.append((target, m[1]))
    return failed if target or run.returncode == 0 else [(("build",), "")]


def persistent(tree, label):
    """The failures at a tree that fail again on one rerun."""
    if tree == TIP and Path(tree, "atom/mesh").is_dir():  # the tests that start atomesh find it here
        built = subprocess.run(["cargo", "build", "--profile", "ci", "--bin", "atomesh", "-q", "-j", "16"], cwd=Path(tree, "atom/mesh"), env=env(tree, label))
        if built.returncode == 0:
            os.environ["ATOMESH_BIN"] = f"{env(tree, label)['CARGO_TARGET_DIR']}/ci/atomesh"
    scope = ["tests/", *(f"--ignore={p}" for p in IGNORE)]
    first = pytest_failures(tree, label, scope)
    again = pytest_failures(tree, f"{label}-rerun", first) if first else []
    found = {f"pytest {t}" for t in first if t in again or t.split("::")[0] in again}
    for args, test in cargo_failures(tree, f"{label}-cargo", ["--lib", "--tests", "--no-fail-fast"]):
        if args != ("build",):
            rerun = cargo_failures(tree, f"{label}-cargo-rerun", [*args, "--", "--exact", test])
            if not rerun:
                continue
        found.add(f"cargo {' '.join(args)} {test}".rstrip())
    os.environ.pop("ATOMESH_BIN", None)
    print(f"{label}: {len(first)} pytest failures, {len(found)} failing again or in cargo", flush=True)
    return found


def git(*args):
    return subprocess.run(["git", *args], cwd=TIP, capture_output=True, text=True, check=True).stdout


def changed_test_modules():
    """atomesh source files that existed at the base and whose test module changed."""
    for path in git("diff", "--name-only", "--diff-filter=M", BASE, "HEAD", "--", "atom/mesh/src").split():
        name = Path(path).name
        if name == "tests.rs" or name.endswith(("_test.rs", "_tests.rs")):
            yield path, "test module"
            continue
        old = git("show", f"{BASE}:{path}").splitlines()
        start = next((i + 1 for i, line in enumerate(old) if line.strip() == "#[cfg(test)]"), None)
        hunks = re.findall(r"^@@ -(\d+)(?:,(\d+))?", git("diff", "-U0", BASE, "HEAD", "--", path), re.M)
        if start and any(int(a) + int(n or 1) > start for a, n in hunks):
            yield path, "its #[cfg(test)] module"


def prs(path):
    subjects = git("log", "--first-parent", "--format=%s", f"{BASE}..HEAD", "--", path).splitlines()
    return ", ".join(sorted({f"#{n}" for s in subjects for n in re.findall(r"\(#(\d+)\)$", s)}, key=lambda p: int(p[1:])))


print(f"tip {git('rev-parse', 'HEAD').strip()} at {TIP}; base {BASE} at {BTREE}; reports in {OUT}", flush=True)
new = sorted(persistent(TIP, "tip") - persistent(BTREE, "base"))
print("new failures:", "none" if not new else "", *(f"\n  {t}" for t in new), sep="")
print("pre-existing tests the tip edited or removed:")
for line in git("diff", "--name-status", "--diff-filter=MD", BASE, "HEAD", "--", "tests/", "atom/mesh/tests/").splitlines():
    status, path = line.split("\t")
    print(f"  {'edited' if status == 'M' else 'removed'} {path}: {prs(path) or 'no PR'}")
for path, what in changed_test_modules():
    print(f"  edited {path}, {what}: {prs(path) or 'no PR'}")
sys.exit(1 if new else 0)
EOF
exec "$PY" -c "$DRIVER"
