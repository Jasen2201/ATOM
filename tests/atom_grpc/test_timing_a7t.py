# SPDX-License-Identifier: MIT
"""The a7t timing case's measures: thread CPU from /proc, and each step handed to gRPC
matched to atomesh's read of it, on a DP4 instance under load over both Sessions."""

import os
import time

import pytest

from atom.entrypoints.grpc.server import StepOutputs
from atom.entrypoints.grpc.session import SEQ_IDS
from tests.atom_grpc.fake.record import stamp_grpc_steps
from tests.atom_grpc.fake.serve import STAMPS_ENV
from tests.atom_grpc.timing import a7t


def test_a_stat_line_gives_its_name_and_user_plus_system_ticks():
    # From field 3, the state; utime and stime are fields 14 and 15.
    fields = "S 1 2 3 0 -1 4194304 10 0 0 0 123 45 0 0 20 0 1"
    assert a7t.stat_ticks(f"4242 (event (eng) x) {fields}\n") == ("event (eng) x", 168)


def test_busy_counts_named_threads_from_the_first_sample_to_each_ones_last():
    samples = [
        {1: ("AtomGrpcServer", 100), 2: ("event_engine", 50), 3: ("uvicorn", 7)},
        {1: ("AtomGrpcServer", 130), 2: ("event_engine", 60), 4: ("event_engine", 5)},
        {1: ("AtomGrpcServer", 150), 3: ("uvicorn", 90), 4: ("event_engine", 9)},
    ]
    # 50 by the server's thread, 10 by one that ended, 9 by one born after the first.
    assert a7t.busy(samples, ("AtomGrpcServer", "event_engine")) == 69


def test_steps_match_by_seq_ids_across_ranks_and_count_from_the_window():
    base = SEQ_IDS  # the first Session's window
    sent = [
        (100, [base | 1, base | 2]),  # before the window
        (200, [base | 3]),  # another rank's, written before this rank's earlier one
        (190, [base | 1, base | 2]),
        (300, [base | 1, base | 2]),
        (400, [base | 3]),  # never read
        (900, [base | 3]),  # past the window
    ]
    arrived = [(150, [1, 2]), (250, [1, 2]), (260, [3]), (330, [1, 2])]
    assert a7t.lags(sent, arrived, 150, 800) == ([60, 60, 30], 1)
    with pytest.raises(ValueError, match="no step was"):
        a7t.lags(sent, [*arrived, (500, [7])], 150, 800)


def test_p99_is_the_nearest_rank():
    assert a7t.p99(list(range(1, 201))) == 198
    assert a7t.p99([5]) == 5


def test_the_grpc_stamp_patches_nothing_unless_its_env_names_a_file(monkeypatch):
    monkeypatch.delenv(STAMPS_ENV, raising=False)
    flush = StepOutputs.flush
    stamp_grpc_steps()
    assert StepOutputs.flush is flush


def test_every_step_handed_to_grpc_reaches_atomesh_over_the_session_on_one_clock(
    tmp_path_factory, monkeypatch
):
    monkeypatch.setattr(a7t, "OUTPUT", 32)  # requests of a few steps: many end
    timing = {"RAMP_S": 1, "WARM_S": 2, "WINDOW_S": 5, "SETTLE_S": 2}
    for name, seconds in timing.items():
        monkeypatch.setattr(a7t, name, seconds)
    every = sorted(os.sched_getaffinity(0))
    cores = {"launcher": every, "atomesh": every, "client": every}
    began = time.monotonic_ns()
    load, sent, arrived = a7t.run(4, 64, True, tmp_path_factory.mktemp("a7t"), cores)
    lags, missed = a7t.lags(sent, arrived, load.start, load.end)
    assert lags and missed == 0
    # Both stamps read CLOCK_MONOTONIC: each lag is under the whole run.
    assert 0 <= min(lags) and max(lags) < time.monotonic_ns() - began
    requests, served = a7t.ended(load.ended)
    assert load.ended and served, requests
    names = {name for sample in load.threads for name, _ in sample.values()}
    assert set(a7t.GRPC_THREADS) <= names
