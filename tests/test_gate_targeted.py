# SPDX-License-Identifier: MIT
"""gate_targeted.sh's selector, .claude/agent-team/select_tests.py, on synthetic diffs."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "select_tests", ROOT / ".claude/agent-team/select_tests.py"
)
select_tests = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(select_tests)

TREE = {
    "pkg/__init__.py": "",
    "pkg/core.py": "",
    "pkg/engine.py": "from . import core\n",
    "pkg/lazy.py": "def load():\n    import pkg.core\n",
    "pkg/reader.py": "TABLE = 'pkg/table.json'\n",
    "pkg/table.json": "{}",
    "tests/conftest.py": "",
    "tests/a/conftest.py": "import pkg.reader\n",
    "tests/a/helper.py": "",
    "tests/a/fixtures/model.toml": "",
    "tests/a/test_engine.py": "from pkg.engine import core\n",
    "tests/a/test_plain.py": "",
    "tests/b/test_lazy.py": "import pkg.lazy\n",
    "tests/b/test_late.py": "def test():\n    import pkg.engine\n",
    "tests/b/test_patch.py": "TARGET = 'pkg.core.handler'\n",
    "tests/b/test_plain.py": "",
}
TESTS = sorted(name for name in TREE if "/test_" in name)
A = ["tests/a/test_engine.py", "tests/a/test_plain.py"]


@pytest.fixture
def tree(tmp_path):
    for name, text in TREE.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return tmp_path


def pick(root, *files, args=("tests",)):
    return select_tests.select_pytest(root, set(files), list(args))


def test_a_module_reaches_the_tests_importing_it_directly_or_through_others(tree):
    # test_lazy is not reached: an import inside a library function is not followed.
    assert pick(tree, "pkg/core.py") == [
        "tests/a/test_engine.py",
        "tests/b/test_late.py",
        "tests/b/test_patch.py",
    ]
    assert pick(tree, "pkg/engine.py") == [
        "tests/a/test_engine.py",
        "tests/b/test_late.py",
    ]
    assert pick(tree, "pkg/__init__.py") == [
        t for t in TESTS if t != "tests/b/test_plain.py"
    ]


def test_a_changed_test_file_reaches_itself(tree):
    assert pick(tree, "tests/b/test_plain.py") == ["tests/b/test_plain.py"]


def test_a_conftest_and_its_imports_reach_the_tests_below_it(tree):
    assert pick(tree, "tests/a/conftest.py") == A
    assert pick(tree, "pkg/reader.py") == A
    assert pick(tree, "tests/conftest.py") == TESTS


def test_a_non_test_file_under_tests_runs_its_directory_whole(tree):
    assert pick(tree, "tests/a/fixtures/model.toml") == A
    assert pick(tree, "tests/a/helper.py") == A
    assert pick(tree, "tests/notes.md") == TESTS


def test_a_file_named_in_a_reached_module_reaches_its_tests(tree):
    assert pick(tree, "pkg/table.json") == A


def test_a_deleted_module_reaches_its_importers(tree):
    (tree / "pkg/engine.py").unlink()
    assert pick(tree, "pkg/engine.py") == [
        "tests/a/test_engine.py",
        "tests/b/test_late.py",
    ]


def test_only_test_files_among_the_arguments_are_picked(tree):
    picked = pick(tree, "pkg/core.py", args=["tests/b/test_patch.py", "tests/a"])
    assert picked == ["tests/a/test_engine.py", "tests/b/test_patch.py"]
    assert pick(tree, "docs/guide.md", "atom/mesh/src/lib.rs") == []


def test_the_repository_maps_a_module_to_the_tests_importing_it():
    picked = pick(
        ROOT, "atom/entrypoints/openai/protocol.py", args=["tests/entrypoints"]
    )
    assert "tests/entrypoints/test_protocol.py" in picked
    assert pick(ROOT, "docs/architecture_guide.md", args=["tests/entrypoints"]) == []


MESH = {
    "api_tests": "mod api;\n",
    "routing_tests": '#[path = "common/mod.rs"]\npub mod common;\nmod routing;\n',
    "spec_test": "mod spec;\n",
}


@pytest.mark.parametrize(
    "change, targets",
    [
        ("atom/mesh/src/lib.rs", ["all"]),
        ("atom/mesh/build.rs", ["all"]),
        ("atom/model_engine/zmq_protocol/engine_v1.proto", ["all"]),
        ("atom/mesh/tests/spec_test.rs", ["spec_test"]),
        ("atom/mesh/tests/api/kv_events_test.rs", ["api_tests"]),
        ("atom/mesh/tests/common/mock_worker.rs", ["routing_tests"]),
        ("atom/mesh/tests/fixtures/golden.bin", sorted(MESH)),
        ("atom/mesh/tests/ext_proc_tests.rs", []),
        ("atom/mesh/mocker/lib.rs", []),
        ("tests/test_gate_targeted.py", []),
    ],
)
def test_mesh_picks_the_targets_a_change_compiles_into(tmp_path, change, targets):
    (tmp_path / "atom/mesh/tests").mkdir(parents=True)
    for name, text in MESH.items():
        (tmp_path / f"atom/mesh/tests/{name}.rs").write_text(text)
    assert select_tests.select_mesh(tmp_path, {change}, sorted(MESH)) == targets


def test_changed_lists_deletions_both_sides_of_a_rename_and_uncommitted_files(
    tmp_path, monkeypatch
):
    def git(*args):
        command = ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args]
        return subprocess.run(
            command, capture_output=True, text=True, check=True
        ).stdout

    monkeypatch.chdir(tmp_path)
    for name in ("kept.py", "edited.py", "gone.py", "old.py"):
        Path(name).write_text(name)
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").strip()
    Path("gone.py").unlink()
    git("mv", "old.py", "new.py")
    git("commit", "-qam", "change")
    Path("edited.py").write_text("edited")
    Path("untracked.py").write_text("")
    changed = {"edited.py", "gone.py", "old.py", "new.py", "untracked.py"}
    assert select_tests.changed(base) == changed
