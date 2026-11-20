"""Which of gate.sh's tests the change since BASE reaches: `pytest BASE ARGS...` prints
the test files among ARGS, `mesh BASE TARGETS...` `all` or the targets among TARGETS."""

import ast
import re
import subprocess
import sys
from pathlib import Path

# pytest's default python_files
TEST_FILE = re.compile(r"test_[^/]*\.py|[^/]*_test\.py")
DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def changed(base: str) -> set[str]:
    """Files changed since `base`, committed or not, deleted ones and both sides of a
    rename included, and untracked files."""
    found = set()
    for args in (
        ["diff", "--name-only", "-z", "--no-renames", base],
        ["ls-files", "-z", "--others", "--exclude-standard"],
    ):
        run = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
        found.update(run.stdout.split("\0"))
    return found - {""}


def module(path: str) -> str | None:
    """The dotted name an import gives the .py file at `path`; None if none can."""
    parts = path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
    named = path.endswith(".py") and parts and all(p.isidentifier() for p in parts)
    return ".".join(parts) if named else None


def packages(mod: str) -> list[str]:
    """The packages an import of `mod` runs first."""
    parts = mod.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


class Graph:
    """What importing each module of the tree at `root` runs: its module-level imports
    (under tests/, every import and dotted string), and the packages above each."""

    def __init__(self, root: Path, gone: set[str]):
        # Deleted modules still resolve, so their importers are reached.
        self.root, self.gone = root, gone
        self.files: dict[str, Path | None] = {}
        self.deps: dict[str, set[str]] = {}
        self.names: dict[str, set[str]] = {}  # the file names its strings end with

    def file(self, mod: str) -> Path | None:
        if mod not in self.files:
            base = self.root.joinpath(*mod.split("."))
            found = (base.with_suffix(".py"), base / "__init__.py")
            self.files[mod] = next((p for p in found if p.is_file()), None)
        return self.files[mod]

    def resolve(self, name: str) -> str | None:
        """The longest prefix of `name` that is a module of the tree or was deleted."""
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            mod = ".".join(parts[:i])
            if mod and (mod in self.gone or self.file(mod)):
                return mod
        return None

    def load(self, mod: str) -> None:
        deps, names, path = set(packages(mod)), set(), self.file(mod)
        self.deps[mod], self.names[mod] = deps, names
        try:
            tree = ast.parse(path.read_bytes()) if path else ast.Module([], [])
        except (SyntaxError, ValueError):
            return
        in_tests = mod.startswith("tests.")
        package = mod if path and path.name == "__init__.py" else mod.rpartition(".")[0]
        nodes = [tree]
        while nodes:
            node = nodes.pop()
            for child in ast.iter_child_nodes(node):
                if in_tests or not isinstance(child, FUNCTIONS):
                    nodes.append(child)
            refs = []
            if isinstance(node, ast.Import):
                refs = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    up = package.split(".")[: package.count(".") + 2 - node.level]
                    base = ".".join([*filter(None, up), *filter(None, [node.module])])
                refs = [base, *(f"{base}.{alias.name}" for alias in node.names)]
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and (not re.search(r"\s", node.value))
            ):
                names.add(node.value.rpartition("/")[2])
                refs = [node.value] if in_tests and DOTTED.fullmatch(node.value) else []
            for ref in refs:
                if (dep := self.resolve(ref)) and dep != mod:
                    deps.add(dep)

    def reach(self, mods: list[str]) -> set[str]:
        """`mods` and every module they run on import, transitively."""
        seen, todo = set(mods), list(mods)
        while todo:
            mod = todo.pop()
            if mod not in self.deps:
                self.load(mod)
            todo += self.deps[mod] - seen
            seen |= self.deps[mod]
        return seen


def select_pytest(root: Path, files: set[str], args: list[str]) -> list[str]:
    """The test files among pytest's `args`, files and directories, that a change to
    `files` reaches."""
    tests = set()
    for arg in args:
        path = root / arg
        found = path.rglob("*.py") if path.is_dir() else [path]
        tests |= {
            p.relative_to(root).as_posix() for p in found if TEST_FILE.fullmatch(p.name)
        }
    mods = {m for f in files if (m := module(f))}
    names = {f.rpartition("/")[2] for f in files}
    # A non-test file under tests/ (a fixture, or a helper a subprocess runs) is mapped
    # to its directory below tests/, or to tests/ itself, whose tests then all run.
    dirs = set()
    for f in files:
        if f.startswith("tests/") and not TEST_FILE.fullmatch(f.rpartition("/")[2]):
            dirs.add("/".join(f.split("/")[:2]) + "/" if f.count("/") > 1 else "tests/")
    graph = Graph(
        root, {m for f in files if (m := module(f)) and not (root / f).exists()}
    )
    picked = []
    for test in sorted(tests):
        here, mod = test.split("/")[:-1], module(test)
        conftests = [
            module("/".join([*here[:i], "conftest.py"])) for i in range(len(here) + 1)
        ]
        reached = graph.reach(
            [mod, *filter(graph.file, filter(None, conftests))] if mod else []
        )
        if (
            not mod
            or test.startswith(tuple(dirs))
            or reached & mods
            or any(graph.names[m] & names for m in reached)
        ):
            picked.append(test)
    return picked


def select_mesh(root: Path, files: set[str], targets: list[str]) -> list[str]:
    """`all` when a change reaches the atomesh library, else the integration targets
    among `targets` that it compiles into."""
    picked = set()
    for f in files:
        rest = f.removeprefix("atom/mesh/tests/")
        if rest == f:
            if f.endswith(".proto") or (
                f.startswith("atom/mesh/") and not f.startswith("atom/mesh/mocker/")
            ):
                return ["all"]
            continue
        top, _, below = rest.partition("/")
        if not below:
            picked |= {top.removesuffix(".rs")} if top.endswith(".rs") else set(targets)
            continue
        # tests/<top>/ is compiled into the targets declaring `mod <top>` or a #[path]
        # into it; a directory no target names (fixtures) runs them all.
        uses = re.compile(rf'\bmod\s+{re.escape(top)}\b|"{re.escape(top)}/')
        users = {
            t
            for t in targets
            if uses.search((root / f"atom/mesh/tests/{t}.rs").read_text())
        }
        picked |= users or set(targets)
    return sorted(picked & set(targets))


if __name__ == "__main__":
    mode, base, *rest = sys.argv[1:]
    select = {"pytest": select_pytest, "mesh": select_mesh}[mode]
    sys.stdout.write("".join(f"{p}\n" for p in select(Path.cwd(), changed(base), rest)))
