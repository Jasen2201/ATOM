#!/bin/bash
# `bash tests/atom_grpc/accept.sh <item>`, an item such as A1 or A7t: runs what acceptance.toml maps to each case,
# prints one line per case and exits 0 only when every case passed, none failed, skipped or missing.
set -euo pipefail
ITEM=${1:?usage: accept.sh <item>, such as A1, A2t or A7t}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
PY=${ATOM_ACCEPT_PYTHON:-/share_nfs/yajizhan/venv-cpu-test/bin/python}
"$PY" -c '' 2>/dev/null || PY=python3  # the timing host runs only run.sh, from its own Python
RUST_ENV=${ATOM_ACCEPT_RUST_ENV:-/dev/shm/yajizhan-rust/env.sh}
if [ -f "$RUST_ENV" ]; then
    . "$RUST_ENV"
    export CARGO_TARGET_DIR=$CARGO_TARGET_DIR-$(basename "$ROOT")  # this tree's own target, as the gate's
fi
OUT=$(mktemp -d "${ATOM_ACCEPT_OUT:-/dev/shm}/accept-$ITEM.XXXXXX")  # short: the fake instances' IPC socket paths
export ACCEPT_ITEM=$ITEM ACCEPT_OUT=$OUT ACCEPT_MAP=${ATOM_ACCEPT_MAP:-$HERE/acceptance.toml}
export TMPDIR=$OUT PYTHONPATH=$ROOT
cd "$ROOT"

read -r -d '' DRIVER <<'EOF' || true
import os, re, subprocess, sys
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    import tomllib
except ImportError:  # Python 3.10 on the timing host
    import tomli as tomllib

ITEM, OUT = os.environ["ACCEPT_ITEM"], os.environ["ACCEPT_OUT"]
spec = tomllib.loads(Path(os.environ["ACCEPT_MAP"]).read_text())
cases = {k: v for k, v in spec["case"].items() if k == ITEM or k.startswith(ITEM + ".")}
if not cases:
    sys.exit(f"accept.sh: acceptance.toml has no case {ITEM} or {ITEM}.<n>")
WORST = ["passed", "skipped", "failed"]


def pytest_outcomes(wanted):
    """Runs every id in `wanted` in one session; each id's outcomes, one per test it selects."""
    import pytest

    class Select:
        def __init__(self):
            self.of = {w: [] for w in wanted}
            self.outcome = {}

        @pytest.hookimpl(tryfirst=True)
        def pytest_collection_modifyitems(self, config, items):
            keep, drop = [], []
            for item in items:
                ids = [w for w in wanted if item.nodeid == w or item.nodeid.startswith((w + "[", w + "::"))]
                for w in ids:
                    self.of[w].append(item.nodeid)
                (keep if ids else drop).append(item)
            config.hook.pytest_deselected(items=drop)
            items[:] = keep

        def pytest_runtest_logreport(self, report):
            now = "skipped" if report.skipped or hasattr(report, "wasxfail") else report.outcome
            self.outcome[report.nodeid] = max(self.outcome.get(report.nodeid, "passed"), now, key=WORST.index)

    if "ATOMESH_BIN" not in os.environ:  # the tests that start atomesh need it: build it as CI does
        subprocess.run(["cargo", "build", "--profile", "ci", "--bin", "atomesh", "-q", "-j", "16"], cwd="atom/mesh", check=True)
        os.environ["ATOMESH_BIN"] = str(Path(os.environ.get("CARGO_TARGET_DIR", "atom/mesh/target"), "ci/atomesh").resolve())
    files = sorted({w.split("::")[0] for w in wanted if Path(w.split("::")[0]).is_file()})
    select = Select()
    if files:
        args = ["-q", "-p", "no:cacheprovider", "--continue-on-collection-errors", "--rootdir=.", f"--basetemp={OUT}/bt"]
        pytest.main([*args, f"--junitxml={OUT}/{ITEM}.xml", *files], plugins=[select])
    return {w: [select.outcome.get(n, "failed") for n in nodes] for w, nodes in select.of.items() if nodes}


def cargo_outcomes(names):
    """Runs atomesh's library tests by exact name; an ignored one counts as skipped."""
    argv = ["cargo", "test", "--features", "atom-engine", "--lib", "-j", "16", "--", "--exact", *names]
    run = subprocess.run(argv, cwd="atom/mesh", capture_output=True, text=True)
    Path(OUT, f"{ITEM}-cargo.log").write_text(run.stdout + run.stderr)
    found = dict(re.findall(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b", run.stdout, re.M))
    suite = ET.Element("testsuite", name=f"{ITEM} cargo")
    for name, result in found.items():
        case = ET.SubElement(suite, "testcase", classname="atomesh", name=name)
        if result != "ok":
            ET.SubElement(case, "failure" if result == "FAILED" else "skipped")
    ET.ElementTree(suite).write(Path(OUT, f"{ITEM}-cargo.xml"))
    word = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}
    return {name: [word[result]] for name, result in found.items()}


def command(name, argv):
    """A case run as one command, passing on exit status 0."""
    return name, [] if subprocess.run(argv).returncode == 0 else [f"{name} failed"]


wanted = [t for c in cases.values() for t in c.get("pytest", [])]
ran = pytest_outcomes(wanted) if wanted else {}
names = [t for c in cases.values() for t in c.get("cargo", [])]
ran |= cargo_outcomes(names) if names else {}
passed = True
for case_id, case in cases.items():
    tests = case.get("pytest", []) + case.get("cargo", [])
    gaps = [f"{t} {o}" for t in tests for o in sorted(set(ran.get(t, ["missing"])) - {"passed"})]
    if "timing" in case:  # on the timing host; run.sh's exit status 2, a refusal, fails the case too
        name, gaps = command(f"tests/atom_grpc/timing/run.sh {case['timing']}", ["bash", "tests/atom_grpc/timing/run.sh", case["timing"]])
        tests = [name]
    if "module" in case:
        name, gaps = command(f"python -m {case['module']}", [sys.executable, "-m", case["module"]])
        tests = [name]
    gaps = gaps if tests else ["no test mapped"]
    passed &= not gaps
    print(f"{case_id} {'failed' if gaps else 'passed'} {', '.join(tests)}".rstrip(), *(f"\n  {g}" for g in gaps), sep="")
for known in spec.get("known", []):
    if any(c == ITEM or c in cases for c in known["cases"]):
        print(f"known: #{known['issue']}: {known['what']}")
print(f"accept.sh {ITEM}: {'PASS' if passed else 'FAIL'}; reports in {OUT}")
sys.exit(0 if passed else 1)
EOF
exec "$PY" -c "$DRIVER"
