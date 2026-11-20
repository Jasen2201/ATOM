# SPDX-License-Identifier: MIT
"""The topology launcher and its session fixture: the single topology serves, each of
its processes holds the cores pinned for it, and none outlives the shutdown."""

import contextlib
import ctypes
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

import tests.atom_grpc.harness.topologies as harness
from tests.atom_grpc.harness.launch import (
    PR_SET_CHILD_SUBREAPER,
    InstanceSpec,
    core_sets,
    load,
)
from tests.atom_grpc.harness.topologies import Topology, by_topology, group_members

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"


def cpu_list(text: str) -> tuple[int, ...]:
    """`0-2,5` as (0, 1, 2, 5)."""
    cpus = []
    for part in text.strip().split(","):
        first, _, last = part.partition("-")
        cpus += range(int(first), int(last or first) + 1)
    return tuple(cpus)


def allowed_cpus(pid: int) -> set[tuple[int, ...]]:
    """Each distinct `Cpus_allowed_list` among the threads of `pid`, read from /proc."""
    found = set()
    for status in Path(f"/proc/{pid}/task").glob("*/status"):
        for line in status.read_text().splitlines():
            if line.startswith("Cpus_allowed_list:"):
                found.add(cpu_list(line.partition(":")[2]))
    return found


@contextlib.contextmanager
def reaping_nothing():
    """This process adopts its descendants' orphans and reaps none of them, as PID 1 of
    a container without an init."""
    libc = ctypes.CDLL(None)
    libc.prctl(PR_SET_CHILD_SUBREAPER, 1)
    try:
        yield
    finally:
        libc.prctl(PR_SET_CHILD_SUBREAPER, 0)


def test_core_sets_are_disjoint_taken_in_order_and_refused_past_the_cpus():
    specs = [
        InstanceSpec("p", "m", [], {}, dp=2, cores={"front_end": 2, "rank": 1}),
        InstanceSpec("unpinned", "m", [], {}),
        InstanceSpec("d", "m", [], {}, cores={"front_end": 1, "rank": 2}),
    ]
    assert core_sets(specs, [10, 11, 12, 13, 14, 15, 16]) == {
        "p": {"front_end": [10, 11], "ranks": [[12], [13]]},
        "unpinned": None,
        "d": {"front_end": [14], "ranks": [[15, 16]]},
    }
    with pytest.raises(ValueError, match="pins 7 CPUs; this process has 6"):
        core_sets(specs, [0, 1, 2, 3, 4, 5])


def test_a_topology_repeating_an_instance_name_is_refused(tmp_path):
    path = tmp_path / "twice.toml"
    table = '[[instance]]\nname = "p"\nmodel = "m"\nargs = []\nfake = {}\n'
    path.write_text(table * 2)
    with pytest.raises(ValueError, match="instance names repeat"):
        load(path)


def test_tests_are_grouped_by_topology_and_the_one_stopping_it_runs_last():
    def serves(topology): ...

    def stops(topology, topologies): ...

    def item(name, topology=None, function=serves):
        marker = topology and SimpleNamespace(args=(topology,))
        closure = ("topology", "topologies") if topology else ()  # as pytest builds it
        return SimpleNamespace(
            name=name,
            get_closest_marker=lambda _: marker,
            fixturenames=closure,
            function=function,
        )

    items = [
        item("a"),
        item("s1", "single"),
        item("stop", "single", stops),
        item("p1", "1p1d"),
        item("b"),
        item("s2", "single"),
        item("p2", "1p1d"),
    ]
    grouped = ["a", "s1", "s2", "stop", "p1", "p2", "b"]
    assert [test.name for test in by_topology(items)] == grouped


def test_the_session_fixture_runs_one_topology_at_a_time(monkeypatch):
    events = []

    class Launch:  # a launcher run that is ready at once
        def __init__(self, path, scratch):
            self.name, self.proc = path.stem, SimpleNamespace(returncode=0)
            events.append(f"start {self.name}")

        def wait_ready(self):
            pass

        def stop(self):
            events.append(f"stop {self.name}")
            return {}

    monkeypatch.setattr(harness, "Topology", Launch)
    topologies = harness.Topologies(SimpleNamespace(mktemp=Path))
    single = topologies.up("single")
    assert topologies.up("single") is single
    topologies.up("1p1d")
    topologies.down()
    topologies.down()
    assert events == ["start single", "stop single", "start 1p1d", "stop 1p1d"]


def test_from_conftest_still_imports_the_tests_conftest():
    import conftest  # as tests/test_*.py do, once this package's conftest has loaded

    assert Path(conftest.__file__).samefile(Path(__file__).parents[1] / "conftest.py")


@pytest.mark.topology("single")
def test_the_single_topology_serves_a_completion(topology):
    (instance,) = topology.instances.values()
    assert instance.get("/health") == (200, {"status": "ok"})
    body = {"prompt_token_ids": [1, 8, 9, 10], "max_tokens": 8, "temperature": 0}
    status, reply = instance.post("/v1/completions", body)
    assert status == 200, reply
    assert reply["choices"][0]["finish_reason"] == "length"
    assert reply["usage"]["completion_tokens"] == 8
    assert {"fake.json", "server.log"} <= {p.name for p in instance.workdir.iterdir()}


@pytest.mark.topology("single")
def test_each_pinned_process_shows_its_core_set_in_proc(topology):
    (instance,) = topology.instances.values()
    front_end, (rank,) = instance.cores["front_end"], instance.cores["ranks"]
    assert not set(front_end) & set(rank)
    api_server = psutil.Process(instance.pid)
    expected = {api_server.pid: front_end}
    for child in api_server.children():
        engine_core = any("EngineCore" in part for part in child.cmdline())
        for process in [child, *child.children(recursive=True)]:
            expected[process.pid] = rank if engine_core else front_end
    assert list(expected.values()).count(rank) >= 2  # the EngineCore and its runner
    assert {pid: allowed_cpus(pid) for pid in expected} == {
        pid: {tuple(cores)} for pid, cores in expected.items()
    }


@pytest.mark.topology("single")
def test_no_process_of_the_topology_outlives_its_shutdown(topology, topologies):
    members = {group: group_members(group) for group in topology.groups()}
    assert all(len(pids) >= 3 for pids in members.values())  # server, core, runner
    with reaping_nothing():  # the launcher reaps its instances' orphans itself
        topologies.down()
    assert topology.proc.returncode == 0
    left = {group: group_members(group) for group in members}
    assert left == {group: [] for group in members}


def test_a_child_dying_before_health_fails_the_launch_with_its_log_tail(
    tmp_path_factory, topologies
):
    topologies.down()  # one topology up at a time
    scratch = tmp_path_factory.mktemp("dies")  # short: ZMQ ipc sockets live in it
    path = scratch / "dies.toml"  # two DP ranks, one rank core set: DP1 cannot pin
    path.write_text(
        f'[[instance]]\nname = "dp2"\nmodel = "{FIXTURE}"\n'
        'args = ["-tp", "2", "--enable-dp-attention"]\n'
        "fake = {}\ncores = { front_end = 1, rank = 1 }\n"
    )
    launch = Topology(path, scratch)
    with pytest.raises(RuntimeError, match=r"(?s)died.*EngineCore-DP1 has no core"):
        launch.wait_ready()
    assert launch.proc.returncode == 1
    assert launch.groups()  # the instance it started before the child died
    assert launch.stop() == {}
