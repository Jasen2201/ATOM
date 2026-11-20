# SPDX-License-Identifier: MIT
"""3P x DP2 + 4D x DP4 through atomesh's gRPC mode with --dp-aware: one command starts
22 fake ranks, and 2000 requests at concurrency 256 run where atomesh sent them."""

import concurrent.futures
import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from transformers import AutoTokenizer

from tests.atom_grpc.fake.record import RECORD, EngineRecord
from tests.atom_grpc.harness.atomesh import LOG
from tests.atom_grpc.harness.launch import load
from tests.atom_grpc.harness.topologies import TOPOLOGIES, group_members

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
MODEL = str(FIXTURE)  # the name a fake instance serves its --model under
INSTANCES, (ROUTER,) = load(TOPOLOGIES / "dp22.toml")
RANKS = {spec.name: spec.dp for spec in INSTANCES}
REQUESTS, CONCURRENCY, MAX_TOKENS = 2000, 256, 4
SYLLABLES = [c + v for c in "bcdfghjklmnprstvwxyz" for v in "aeiou"]  # one id each
SENT = "Sending concurrent requests"  # atomesh's debug event: the pair it sends to


@pytest.fixture(scope="module")
def tokenizer():
    return AutoTokenizer.from_pretrained(FIXTURE)


def prompt(i: int) -> str:
    """Request i: nine words, the first two its own. It fills no KV block whole, which
    relay fails on (#71), and shares too little for atomesh's cache affinity."""
    return " ".join([SYLLABLES[i % 100], SYLLABLES[i // 100], *SYLLABLES[50:57]])


def complete(router, text: str) -> tuple[int, object]:
    body = {"model": MODEL, "prompt": text, "max_tokens": MAX_TOKENS}
    return router.post("/v1/completions", body)


def url_at_rank(topology, name: str, rank: int) -> str:
    return f"grpc://{topology.instances[name].grpc}@{rank}"


def every_rank(topology) -> set[str]:
    return {url_at_rank(topology, n, r) for n, dp in RANKS.items() for r in range(dp)}


def sent(topology) -> Counter:
    """Each (P, D) url@rank pair atomesh has logged sending a request to."""
    pairs = Counter()
    for line in (topology.scratch / ROUTER.name / LOG).read_text().splitlines():
        event = json.loads(line) if line.startswith("{") else {}
        if event.get("message") == SENT:
            pairs[event["prefill_url"], event["decode_url"]] += 1
    return pairs


def ran(topology, names: list[str]) -> dict[tuple, list[str]]:
    """Each prompt's admissions on the instances `names`, as the url@rank its request
    named, once every step of it ran on that rank."""
    found = defaultdict(list)
    for name in names:
        record = EngineRecord(topology.instances[name].workdir / RECORD)
        for request in record.requests.values():
            rank = request.data_parallel_rank
            assert rank is not None and request.steps, request
            assert {step.dp_rank for step in request.steps} == {rank}, request
            found[tuple(request.prompt_ids)].append(url_at_rank(topology, name, rank))
    return found


@pytest.mark.topology("dp22")
def test_the_launch_command_starts_22_healthy_ranks_that_serve_through_atomesh(
    topology,
):
    router = topology.routers[ROUTER.name]
    for instance in topology.instances.values():
        assert instance.get("/health") == (200, {"status": "ok"})
    status, listing = router.get("/workers")
    assert status == 200, listing
    workers = {
        (w["url"], w["connection_mode"], w["runtime_type"], w["is_healthy"])
        for w in listing["workers"]
    }
    assert workers == {(url, "gRPC", "atom", True) for url in every_rank(topology)}
    status, reply = complete(router, " ".join(SYLLABLES[:5]))
    assert status == 200, reply
    assert reply["usage"]["completion_tokens"] == MAX_TOKENS


@pytest.mark.topology("dp22")
def test_2000_requests_at_concurrency_256_reach_every_rank_each_where_atomesh_sent_it(
    topology, tokenizer
):
    router, before = topology.routers[ROUTER.name], sent(topology)
    texts = [prompt(i) for i in range(REQUESTS)]
    with concurrent.futures.ThreadPoolExecutor(CONCURRENCY) as pool:
        replies = list(pool.map(lambda text: complete(router, text), texts))
    for status, reply in replies:
        assert status == 200, reply
        assert reply["usage"]["completion_tokens"] == MAX_TOKENS, reply

    prefills, decodes = ran(topology, ROUTER.prefill), ran(topology, ROUTER.decode)
    pairs = Counter()
    for text in texts:
        ids = tuple(tokenizer.encode(text))
        legs = prefills[ids] + decodes[ids]
        assert len(prefills[ids]) == len(decodes[ids]) == 1, (text, legs)
        pairs[tuple(legs)] += 1
    assert sent(topology) == before + pairs  # each request ran where atomesh sent it
    assert {url for pair in pairs for url in pair} == every_rank(topology)


@pytest.mark.topology("dp22")
def test_no_process_of_the_22_ranks_or_atomesh_outlives_the_shutdown(
    topology, topologies
):
    groups = [*topology.groups(), *topology.groups("routers")]
    assert len(groups) == len(RANKS) + 1 and all(map(group_members, groups))
    topologies.down()
    assert topology.proc.returncode == 0
    assert [pid for group in groups for pid in group_members(group)] == []
