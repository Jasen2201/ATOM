# SPDX-License-Identifier: MIT
"""The step law on a fake instance: steps cost the test law's prices, back to back on one
virtual clock, each output handed back when ATOM's deferred output makes it due."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path

import pytest

from tests.atom_grpc.fake.instance import FakeInstance
from tests.atom_grpc.fake.law import Group, Shape, StepLaw
from tests.atom_grpc.fake.record import EngineRecord
from tests.atom_grpc.fake.runner import Clock

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LAW = FIXTURES / "laws" / "test.json"
ARGS = ["--method", "mtp", "--num-speculative-tokens", "3"]
ARGS += ["--cudagraph-capture-sizes", "[1,2,4]", "--max-num-seqs", "8"]
ARGS += ["--max-num-batched-tokens", "32"]  # a 40-token prompt prefills in two chunks
# ARGS on two DP ranks, with a budget that prefills a 40-token prompt at once.
DP2 = ["-tp", "2", "--enable-dp-attention", *ARGS[:-1], "64"]
PRODUCER = [*DP2, "--kv-transfer-config"]  # DP2 as a P/D producer
PRODUCER += [json.dumps({"kv_connector": "mooncake", "kv_role": "kv_producer"})]
STARTUP_S = 1500  # a loaded host takes minutes
WAIT_S = 120  # for a record line on a loaded host
EXACT = 1e-12  # relative: the same terms, summed in another order
# A DP dummy's prices, which run no draft-KV pass: a decode row of 4 at rung 1 or 2,
# the target's terms then the draft's; one prefill token in bucket 64, then a propose.
DUMMY_DECODE = {
    1: 0.005 + 4 * 1e-6 + 6e-4 + 4 * 1e-8,
    2: 0.006 + 4 * 1e-6 + (2 * 4 - 4) * 2e-7 + 7e-4 + 4 * 1e-8,
}
DUMMY_PREFILL = 0.004 + 1e-5 + 1e-8 + 5e-4 + 2e-3 + (64 - 1) * 4e-6 + 7e-4 + 8e-5


@pytest.fixture(scope="module")
def instance(tmp_path_factory):
    fake = {"acceptance_length": 3.51, "law": str(LAW), "clock": "virtual"}
    workdir = tmp_path_factory.mktemp("law")
    started = FakeInstance(workdir, FIXTURES / "deepseek_v3_mtp", ARGS, fake)
    try:
        started.start()
        started.wait_ready(time.monotonic() + STARTUP_S)
        yield started
    finally:
        started.stop()


@pytest.fixture(scope="module")
def dp2(tmp_path_factory):
    fake = {"acceptance_length": 3.51, "law": str(LAW), "clock": "virtual"}
    workdir = tmp_path_factory.mktemp("dp2")
    started = FakeInstance(workdir, FIXTURES / "deepseek_v3_mtp", DP2, fake)
    try:
        started.start()
        started.wait_ready(time.monotonic() + STARTUP_S)
        yield started
    finally:
        started.stop()


@pytest.fixture
def producer(tmp_path_factory):
    """One test's own: no D takes the KV it holds, so its group steps until it stops."""
    fake = {"acceptance_length": 3.51, "law": str(LAW), "clock": "virtual"}
    fake["block_bytes"] = 1  # what a P instance must set; no KV leaves this one
    workdir = tmp_path_factory.mktemp("producer")
    started = FakeInstance(workdir, FIXTURES / "deepseek_v3_mtp", PRODUCER, fake)
    try:
        started.start()
        started.wait_ready(time.monotonic() + STARTUP_S)
        yield started
    finally:
        started.stop()


def complete(instance, prompt: list[int]) -> int:
    """Its seq id, once a 20-token completion of `prompt` is answered."""
    body = {"prompt_token_ids": prompt, "max_tokens": 20, "temperature": 0}
    status, reply = instance.post("/v1/completions", body)
    assert status == 200, reply
    (request,) = EngineRecord(instance.record).with_prompt(prompt)
    return request.seq_id


def test_a_requests_steps_cost_the_test_laws_prices_for_prefill_decode_and_draft(
    instance,
):
    seq = complete(instance, [1, *range(8, 47)])
    forwards = EngineRecord(instance.record).forwards
    middle, final, *decodes = [f for f in forwards if f.requests[0][0] == seq]

    # 32 tokens fill bucket 32 and sample nothing; the draft's pass fills its KV.
    assert (middle.requests, middle.samples) == ([[seq, 32, 32, False]], False)
    target = 0.004 + 32 * 1e-5 + 32 * 32 * 1e-8
    assert middle.price == pytest.approx(target + 1e-4 + 32 * 2e-6, rel=EXACT)

    # 8 tokens on 32 cached, padded to bucket 32; the draft's prefill pass runs as the
    # target's, then the drafter proposes.
    assert (final.requests, final.samples) == ([[seq, 8, 40, False]], True)
    target = 0.004 + 8 * 1e-5 + 8 * 8 * 1e-8 + 8 * 32 * 2e-8 + 32 * 3e-7
    target += 5e-4 + 2e-3 + (32 - 8) * 4e-6
    draft_kv = 1e-4 + 8 * 2e-6 + 32 * 1e-7 + 3e-5 + 4e-5
    assert final.price == pytest.approx(target + draft_kv + 7e-4 + 8e-5, rel=EXACT)

    # Each verify of 4 rows replays rung 1: the target, its draft-KV pass, the draft.
    assert decodes
    for step in decodes:
        ((_, query, context, decode),) = step.requests
        assert (query, decode, step.rung, step.samples) == (4, True, 1, True)
        price = 0.005 + context * 1e-6 + 5e-5 + 3e-6 + 6e-4 + context * 1e-8
        assert step.price == pytest.approx(price, rel=EXACT)


def test_steps_run_back_to_back_and_hand_back_each_output_once_it_is_ready(instance):
    complete(instance, [1, *range(50, 89)])
    forwards = EngineRecord(instance.record).forwards
    assert any(not f.samples for f in forwards)
    ready = 0.0  # the end of the last sampling step, whose output the next one returns
    for step, after in pairwise(forwards):
        assert step.time == step.start + step.price
        assert after.start == step.time  # the virtual clock's engine always keeps up
        if step.samples:  # deferred output: the step before's, once it ended
            assert after.called == max(step.called, ready)
            ready = step.time
        else:  # a middle chunk returns at once
            assert after.called == step.called
    assert any(after.called > step.called for step, after in pairwise(forwards))


def on_rank_0(prompt: list[int], max_tokens: int) -> dict:
    body = {"prompt_token_ids": prompt, "max_tokens": max_tokens, "temperature": 0}
    return body | {"data_parallel_rank": 0}


def test_a_dp2_ranks_dummies_cost_the_row_they_run_at_the_groups_phase_and_rung(dp2):
    with ThreadPoolExecutor(1) as pool:
        # One request decodes alone on rank 0, then beside a second one.
        body = on_rank_0([1, *range(8, 47)], 400)
        first = pool.submit(dp2.post, "/v1/completions", body)
        while not any(
            f.dp_rank == 1 and f.requests[0][3]
            for f in EngineRecord(dp2.record).forwards
        ):
            assert not first.done(), first.result()
            time.sleep(0.01)
        second = dp2.post("/v1/completions", on_rank_0([1, *range(50, 89)], 20))
        for status, reply in (second, first.result()):
            assert status == 200, reply

    forwards = EngineRecord(dp2.record).forwards
    busy, idle = ([f for f in forwards if f.dp_rank == rank] for rank in (0, 1))
    for step, dummy in zip(busy, idle):  # each group step is one forward a rank
        assert dummy.requests[0][0] is None and dummy.samples
        if step.requests[0][3]:  # rank 0 decodes: a decode row of 4 at the group's rung
            assert (dummy.requests, dummy.rung) == ([[None, 4, 4, True]], step.rung)
            assert dummy.price == pytest.approx(DUMMY_DECODE[step.rung], rel=EXACT)
        else:  # rank 0 prefills 40 tokens: one token, in the bucket that holds them
            assert [row[1] for row in step.requests] == [40]
            assert (dummy.requests, dummy.rung) == ([[None, 1, 1, False]], None)
            assert dummy.price == pytest.approx(DUMMY_PREFILL, rel=EXACT)
    kinds = {(dummy.requests[0][3], dummy.rung) for dummy in idle}
    assert kinds == {(False, None), (True, 1), (True, 2)}  # beside each step kind


def test_a_dp2_producer_prices_its_eager_decode_and_idle_groups_dummies_at_0_s(
    producer,
):
    prompt = [1, *range(8, 47)]
    kv = {"do_remote_decode": True, "do_remote_prefill": False}
    body = on_rank_0(prompt, 1) | {"kv_transfer_params": kv}
    status, reply = producer.post("/v1/completions", body)
    assert status == 200, reply
    # Once its request leaves, its group runs dummies while the KV it holds waits.
    deadline = time.monotonic() + WAIT_S
    while True:
        record = EngineRecord(producer.record)
        busy, idle = ([f for f in record.forwards if f.dp_rank == r] for r in (0, 1))
        if min(len(busy), len(idle)) > 2:
            break
        assert time.monotonic() < deadline, record.forwards
        time.sleep(0.01)

    (request,) = record.with_prompt(prompt)
    (prefill, decode, *after), (prefill_dummy, *dummies) = busy, idle
    assert prefill.requests == [[request.seq_id, 40, 40, False]]
    assert prefill_dummy.price == pytest.approx(DUMMY_PREFILL, rel=EXACT)
    # Deferred output decodes it once more, at q=1: eagerly, as its recordings hold q=4.
    assert (decode.requests, decode.rung) == ([[request.seq_id, 1, 41, True]], None)
    assert decode.price == 0.0
    # Rank 1's dummy beside that decode, then the idle group's dummies.
    for step in [*after, *dummies]:
        assert step.requests == [[None, 1, 1, True]]
        assert (step.rung, step.price) == (None, 0.0)


def test_the_port_prices_padded_decode_rows_and_two_finishing_prompts_by_hand():
    law = StepLaw.load(str(LAW))
    # Rung 4 pads two decode rows to four of the widest context.
    target = 0.007 + 120 * 1e-6 + (4 * 70 - 120) * 2e-7
    price = target + 5e-5 + 2 * 3e-6 + 8e-4 + 120 * 1e-8
    decode = Shape(((4, 50, True), (4, 70, True)), 4, True)
    assert law.price(decode) == pytest.approx(price, rel=EXACT)
    # 30 tokens on 10 cached and 20 new, padded to bucket 64.
    target = 0.004 + 50 * 1e-5 + (30 * 30 + 20 * 20) * 1e-8 + 30 * 10 * 2e-8
    target += 10 * 3e-7 + 2 * 5e-4 + 2e-3 + (64 - 50) * 4e-6
    draft_kv = 1e-4 + 50 * 2e-6 + 10 * 1e-7 + 2 * 3e-5 + 4e-5
    prefill = Shape(((30, 40, False), (20, 20, False)), None, True)
    price = target + draft_kv + 7e-4 + 2 * 8e-5
    assert law.price(prefill) == pytest.approx(price, rel=EXACT)
    # Past the largest bucket a chunk runs eagerly, with no padding.
    middle = Shape(((200, 200, False),), None, False)
    price = 0.004 + 200 * 1e-5 + 200 * 200 * 1e-8 + 1e-4 + 200 * 2e-6
    assert law.price(middle) == pytest.approx(price, rel=EXACT)


def test_the_port_prices_a_dp_ranks_step_by_its_group():
    law = StepLaw.load(str(LAW))
    # 8 tokens pad to bucket 64, which holds a peer's 40.
    target = 0.004 + 8 * 1e-5 + 8 * 8 * 1e-8 + 5e-4 + 2e-3 + (64 - 8) * 4e-6
    draft_kv = 1e-4 + 8 * 2e-6 + 3e-5 + 4e-5
    prefill = Shape(((8, 8, False),), None, True, Group(40, False, True, False))
    price = target + draft_kv + 7e-4 + 8e-5
    assert law.price(prefill) == pytest.approx(price, rel=EXACT)
    # Replaying no graph beside a prefilling peer or as a dummy, a decode costs 0 s; so
    # does an idle group's dummy at a rung with no vector, not at a rung with one.
    beside = Shape(((4, 9, True),), None, True, Group(40, False, True, False))
    eager = Shape(((4, 4, True),), None, True, Group(0, True, False, False))
    idle = Shape(((4, 4, True),), 8, True, Group(0, True, False, True))
    assert law.price(beside) == law.price(eager) == law.price(idle) == 0.0
    assert law.price(idle._replace(rung=1)) == pytest.approx(DUMMY_DECODE[1], rel=EXACT)
    # A law with an eager vector prices them by it.
    with_eager = json.loads(LAW.read_text())
    for kind in (with_eager, with_eager["draft"]):
        kind["decode"]["eager"] = kind["decode"]["1"]
    price = 0.005 + 9 * 1e-6 + 5e-5 + 3e-6 + 6e-4 + 9 * 1e-8
    assert StepLaw(with_eager).price(beside) == pytest.approx(price, rel=EXACT)


def test_the_port_prices_a_producers_eager_decode_at_0_s_or_by_an_eager_vector():
    law = json.loads(LAW.read_text())
    decode = Shape(((1, 41, True),), None, True, producer=True)  # one rank: no peer
    assert StepLaw(law).price(decode) == 0.0
    for kind in (law, law["draft"]):
        kind["decode"]["eager"] = kind["decode"]["1"]
    price = 0.005 + 41 * 1e-6 + 5e-5 + 3e-6 + 6e-4 + 41 * 1e-8
    assert StepLaw(law).price(decode) == pytest.approx(price, rel=EXACT)


@pytest.mark.parametrize(
    "edit, shape, refusal",
    [
        (None, Shape(((4, 9, True), (1, 1, False)), 2, True), "mixing"),
        (None, Shape(((4, 9, True),) * 8, None, True), "rung eager"),
        (("decode", "2", "moe_tokens"), None, "moe_tokens"),
    ],
)
def test_the_port_refuses_what_it_cannot_price(edit, shape, refusal):
    law = json.loads(LAW.read_text())
    if edit is not None:
        kind, rung, feature = edit
        law[kind][rung]["coefficients"][feature] = 1e-6
    with pytest.raises(ValueError, match=refusal):
        StepLaw(law).price(shape)


class Preempted:
    """True time, 1 us a read, with a 5 s preemption before read `at`; wall = it + 100."""

    def __init__(self, at: int):
        self.t, self.reads, self.at = 0.0, 0, at

    def monotonic(self) -> float:
        self.reads += 1
        self.t += 5.0 if self.reads == self.at else 1e-6
        return self.t

    def wall(self) -> float:
        return self.monotonic() + 100.0


@pytest.mark.parametrize("at", range(1, 10))
def test_a_preemption_between_clock_reads_does_not_move_a_wall_stamp(at):
    time_source = Preempted(at)
    clock = Clock(time_source.monotonic, time_source.wall)
    assert clock.offset() == pytest.approx(100.0, abs=1e-5)
