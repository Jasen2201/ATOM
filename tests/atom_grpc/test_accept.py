# SPDX-License-Identifier: MIT
"""accept.sh runs the tests acceptance.toml maps to an item's cases and passes a case only
when all of them passed; regression.sh reports the failures and test edits a tip adds.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MAP = tomllib.loads((HERE / "acceptance.toml").read_text())
FRAMES = "tests/atom_grpc/test_frames.py"
NO_RUST = (
    "/nonexistent/env.sh"  # cargo comes from PATH, as a stub where a test gives one
)


def accept(item: str, tree: Path, out: Path, **env) -> tuple[int, list[str]]:
    """accept.sh's exit status and the lines it prints after the tests' own output."""
    env = (
        os.environ
        | {
            "ATOM_ACCEPT_PYTHON": sys.executable,
            "ATOM_ACCEPT_RUST_ENV": NO_RUST,
            "ATOM_ACCEPT_OUT": str(out),
            "ATOMESH_BIN": "/bin/true",  # no test here starts atomesh
        }
        | env
    )
    script = tree / "tests/atom_grpc/accept.sh"
    run = subprocess.run(
        ["bash", script, item],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    lines = run.stdout.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith(item))
    return run.returncode, lines[first:]


def test_a_case_passes_when_every_test_it_maps_passed_and_fails_on_a_missing_one(
    tmp_path,
):
    mapped = tmp_path / "map.toml"
    mapped.write_text(f"""
[case."X1.1"]
pytest = [
    "{FRAMES}::test_submit_batch_and_abort_round_trip",
    "{FRAMES}::test_a_message_of_another_protocol_version_is_refused",
]
[case."X1.2"]
pytest = ["{FRAMES}::test_submit_batch_and_abort_round_trip", "{FRAMES}::test_gone"]
[case."X12.1"]
pytest = ["{FRAMES}::test_gone"]
""")
    status, lines = accept("X1", ROOT, tmp_path, ATOM_ACCEPT_MAP=str(mapped))
    assert status == 1
    assert lines[0] == (
        f"X1.1 passed {FRAMES}::test_submit_batch_and_abort_round_trip,"
        f" {FRAMES}::test_a_message_of_another_protocol_version_is_refused"
    )
    assert lines[1].startswith("X1.2 failed ")
    assert lines[2] == f"  {FRAMES}::test_gone missing"
    assert lines[3].startswith("accept.sh X1: FAIL")
    # One JUnit report; a function's id runs each of its parameters, here 2.
    (report,) = tmp_path.glob("accept-X1.*/X1.xml")
    assert len(list(ET.parse(report).iter("testcase"))) == 3


@pytest.fixture
def stub(tmp_path) -> Path:
    """A tree holding accept.sh beside stub tests, a stub cargo, run.sh and modules."""
    tree = tmp_path / "tree"
    (tree / "tests/atom_grpc/timing").mkdir(parents=True)
    (tree / "atom/mesh").mkdir(parents=True)
    shutil.copy(HERE / "accept.sh", tree / "tests/atom_grpc")
    (tree / "tests/test_stub.py").write_text("""
import pytest

def test_passes(): pass
def test_fails(): assert False
def test_skips(): pytest.skip("absent")
@pytest.mark.xfail(strict=False)
def test_xfails(): assert False
@pytest.fixture
def broken(): raise RuntimeError
def test_errors_in_setup(broken): pass
@pytest.mark.parametrize("ok", [True, False])
def test_one_param_fails(ok): assert ok
""")
    (tree / "tests/atom_grpc/timing/run.sh").write_text('echo "$1" >>ran; exit $1\n')
    for code in (0, 1):
        (tree / f"stub_{code}.py").write_text(f"raise SystemExit({code})\n")
    cargo = tmp_path / "bin/cargo"
    cargo.parent.mkdir()
    cargo.write_text("""#!/bin/sh
echo "$@" >>../../cargo-args
echo "test a::passes ... ok"
echo "test a::fails ... FAILED"
echo "test a::ignored ... ignored, a benchmark"
""")
    cargo.chmod(0o755)
    (tree / "tests/atom_grpc/acceptance.toml").write_text("""
[case."Y1.1"]
pytest = ["tests/test_stub.py::test_passes"]
cargo = ["a::passes"]
[case."Y1.2"]
pytest = ["tests/test_stub.py::test_fails"]
[case."Y1.3"]
pytest = ["tests/test_stub.py::test_skips", "tests/test_stub.py::test_xfails"]
[case."Y1.4"]
pytest = ["tests/test_stub.py::test_errors_in_setup"]
[case."Y1.5"]
pytest = ["tests/test_stub.py::test_one_param_fails"]
[case."Y1.6"]
cargo = ["a::fails", "a::ignored", "a::gone"]
[case."Y1.7"]
[case."Y2.1"]
module = "stub_0"
[case."Y2.2"]
module = "stub_1"
[case.Y3t]
timing = "0"
[case.Y4t]
timing = "2"
[[known]]
issue = 7
cases = ["Y1.2", "Y3t"]
what = "a known defect"
""")
    return tree


def test_a_failed_skipped_xfailed_errored_or_missing_test_fails_its_case(
    stub, tmp_path
):
    path = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    status, lines = accept("Y1", stub, tmp_path, PATH=path)
    assert status == 1
    stub_tests = "tests/test_stub.py::test_"
    assert lines == [
        f"Y1.1 passed {stub_tests}passes, a::passes",
        f"Y1.2 failed {stub_tests}fails",
        f"  {stub_tests}fails failed",
        f"Y1.3 failed {stub_tests}skips, {stub_tests}xfails",
        f"  {stub_tests}skips skipped",
        f"  {stub_tests}xfails skipped",
        f"Y1.4 failed {stub_tests}errors_in_setup",
        f"  {stub_tests}errors_in_setup failed",
        f"Y1.5 failed {stub_tests}one_param_fails",
        f"  {stub_tests}one_param_fails failed",
        "Y1.6 failed a::fails, a::ignored, a::gone",
        "  a::fails failed",
        "  a::ignored skipped",
        "  a::gone missing",
        "Y1.7 failed",
        "  no test mapped",
        "known: #7: a known defect",
        lines[-1],
    ]
    assert lines[-1].startswith("accept.sh Y1: FAIL; reports in ")
    # One cargo run for the item, by exact name, as the library tests of atom-engine.
    args = (stub / "cargo-args").read_text().split()
    assert args[:4] == ["test", "--features", "atom-engine", "--lib"]
    assert args[args.index("--exact") + 1 :] == ["a::passes", "a::fails"] + [
        "a::ignored",
        "a::gone",
    ]


def test_a_module_case_and_a_timing_case_pass_only_on_exit_status_0(stub, tmp_path):
    status, lines = accept("Y2", stub, tmp_path)
    assert (status, lines[:3]) == (
        1,
        ["Y2.1 passed python -m stub_0", "Y2.2 failed python -m stub_1"]
        + ["  python -m stub_1 failed"],
    )
    status, lines = accept("Y3t", stub, tmp_path)
    assert (status, lines[:2]) == (
        0,
        ["Y3t passed tests/atom_grpc/timing/run.sh 0", "known: #7: a known defect"],
    )
    status, lines = accept("Y4t", stub, tmp_path)  # run.sh refusing to run
    assert (status, lines[1]) == (1, "  tests/atom_grpc/timing/run.sh 2 failed")
    assert (stub / "ran").read_text().split() == ["0", "2"]
    assert not (stub / "cargo-args").exists()


def test_every_pytest_id_the_map_names_selects_tests_this_tree_collects():
    ids = {i for case in MAP["case"].values() for i in case.get("pytest", [])}
    files = sorted({i.split("::")[0] for i in ids})
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ]
        + ["--rootdir=.", *files],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    collected = [line for line in run.stdout.splitlines() if "::" in line]
    unmatched = [
        i for i in ids if not any(c == i or c.startswith(i + "[") for c in collected)
    ]
    assert run.returncode == 0 and not unmatched, run.stdout[-2000:]
    # Each case runs something; the cases of an item run as one session.
    assert all(
        {"pytest", "cargo", "module", "timing"} & set(c) for c in MAP["case"].values()
    )


@pytest.fixture
def toy(tmp_path) -> tuple[Path, str]:
    """A repository whose tip edits a test and adds a failure, a flaky test and a PR."""
    repo = tmp_path / "repo"
    (repo / "tests/atom_grpc").mkdir(parents=True)
    shutil.copy(HERE / "regression.sh", repo / "tests/atom_grpc")
    old = "def test_kept(): pass\ndef test_broken(): assert False\n"
    (repo / "tests/test_old.py").write_text(old)
    (repo / "tests/test_removed.py").write_text("def test_gone(): pass\n")

    def commit(message):
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "commit",
                "-qm",
                message,
            ],
            cwd=repo,
            check=True,
        )
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()

    subprocess.run(["git", "init", "-q", repo], check=True)
    base = commit("base")
    (repo / "tests/test_old.py").write_text(old + "def test_more(): pass\n")
    (repo / "tests/test_removed.py").unlink()
    (repo / "tests/test_new.py").write_text("""
from pathlib import Path

def test_new_fails(): assert False

def test_flaky():
    mark = Path(__file__).with_name("flaked")
    first = not mark.exists()
    mark.touch()
    assert not first  # fails on its first run only
""")
    commit("Tip work (#7)")
    return repo, base


def test_regression_reports_only_a_failure_the_base_lacks_and_the_tests_the_tip_edits(
    toy, tmp_path
):
    repo, base = toy
    env = os.environ | {
        "ATOM_ACCEPT_PYTHON": sys.executable,
        "ATOM_ACCEPT_RUST_ENV": NO_RUST,
        "REGRESSION_BASE": base,
        "REGRESSION_BASE_TREE": str(tmp_path / "base"),
        "REGRESSION_OUT": str(tmp_path),
    }
    run = subprocess.run(
        ["bash", repo / "tests/atom_grpc/regression.sh"],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    lines = run.stdout.splitlines()
    start = lines.index("new failures:")
    assert lines[start:] == [
        "new failures:",
        "  pytest tests/test_new.py::test_new_fails",
        "pre-existing tests the tip edited or removed:",
        "  edited tests/test_old.py: #7",
        "  removed tests/test_removed.py: #7",
    ]
