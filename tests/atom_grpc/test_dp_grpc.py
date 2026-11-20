# SPDX-License-Identifier: MIT
"""DP P/D through atomesh's gRPC mode with --dp-aware on fake P DP2 and D DP4: the sizes
each reports, where requests run beside the HTTP path, loads, faults, cache affinity."""

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from pathlib import Path

import grpc
import pytest
from transformers import AutoTokenizer

from atom.entrypoints.grpc import atom_grpc_pb2 as pb
from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from tests.atom_grpc.fake.record import RECORD, EngineRecord
from tests.atom_grpc.harness.launch import load
from tests.atom_grpc.harness.topologies import TOPOLOGIES

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
MODEL = str(FIXTURE)  # the name a fake instance serves its --model under
INSTANCES, ROUTERS = load(TOPOLOGIES / "dp.toml")
(FAULT,) = next(spec.fake["faults"] for spec in INSTANCES if spec.name == "d")
SIZES = {"prelay": (2, 1), "pchunked": (2, 1), "d": (4, 1)}  # (dp_size, tp_size)
SYLLABLES = [c + v for c in "bcdfghjklmnprstvwxyz" for v in "aeiou"]  # fixture words
XFER = re.compile(r"xfer-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")  # a chunked id
WAIT_S = 120
# Each request's session and the (P, D) ranks a mapping's routers pick: P round-robin,
# D a new rank per new session; idx2idx moves P to D's rank where P has that rank.
SESSIONS = ["a", "a", "b", "c", "d"]
RANKS = {
    "none": [(0, 0), (1, 0), (0, 1), (1, 2), (0, 3)],
    "idx2idx": [(0, 0), (0, 0), (1, 1), (1, 2), (0, 3)],
}


@pytest.fixture(scope="module")
def tokenizer():
    return AutoTokenizer.from_pretrained(FIXTURE)


def words(count: int, lead: int) -> str:
    """`count` fixture words from the `lead`-th on, one id each."""
    return " ".join(SYLLABLES[(lead + i) % len(SYLLABLES)] for i in range(count))


def server_info(instance) -> pb.GetServerInfoResponse:
    with grpc.insecure_channel(instance.grpc) as channel:
        stub = pb_grpc.AtomEngineStub(channel)
        return stub.GetServerInfo(pb.GetServerInfoRequest(), timeout=60)


def request(router, prompt: str, session: str | None = None, **fields):
    """A `/v1/completions` call to `router` for urllib, in `session` if given."""
    body = {"model": MODEL, "prompt": prompt, "max_tokens": 4, "temperature": 0}
    headers = {"Content-Type": "application/json"}
    if session is not None:
        headers["X-Session-ID"] = session
    data = json.dumps(body | fields).encode()
    return urllib.request.Request(router.url + "/v1/completions", data, headers)


def complete(router, prompt: str, session: str | None = None, **fields):
    """Its status and reply: the completion, or the error text."""
    try:
        call = request(router, prompt, session, **fields)
        with urllib.request.urlopen(call, timeout=600) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def loads(router) -> dict[str, int]:
    """atomesh's in-flight count of each url@rank."""
    status, listing = router.get("/workers")
    assert status == 200, listing
    return {worker["url"]: worker["load"] for worker in listing["workers"]}


def idle(router) -> bool:
    return not any(loads(router).values())


def wait_for(condition, what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"no {what} within {WAIT_S} s")
        time.sleep(0.2)


def admitted(instance, prompt_ids: list[int]) -> list:
    return EngineRecord(instance.workdir / RECORD).with_prompt(prompt_ids)


def rank(request) -> int:
    """The DP rank `request` was admitted to, once every step it took ran there."""
    ran = {step.dp_rank for step in request.steps}
    assert ran == {request.data_parallel_rank}, (request.data_parallel_rank, ran)
    return request.data_parallel_rank


def by_form(kv: dict) -> dict:
    """`kv` with what P assigns each request as its form: the transfer id (an xfer id,
    or relay's P request id) and relay's block ids."""
    form = dict(kv)
    if "transfer_id" in kv:
        chunked = XFER.fullmatch(str(kv["transfer_id"]))
        form["transfer_id"] = "xfer" if chunked else type(kv["transfer_id"]).__name__
    if "remote_block_ids" in kv:
        form["remote_block_ids"] = len(kv["remote_block_ids"])
    return form


@pytest.mark.topology("dp")
@pytest.mark.parametrize("name", SIZES)
def test_each_instance_reports_its_dp_and_tp_size_over_grpc_and_http(topology, name):
    instance = topology.instances[name]
    info = server_info(instance)
    reports = [
        json.loads(info.server_info_json),
        json.loads(info.kv_transfer_info_json),
    ]
    for route in ("/server_info", "/kv_transfer_info"):
        status, report = instance.get(route)
        assert status == 200, report
        reports.append(report)
    assert [(r["dp_size"], r["tp_size"]) for r in reports] == [SIZES[name]] * 4


@pytest.mark.topology("dp")
@pytest.mark.parametrize("name", [router.name for router in ROUTERS if router.grpc])
def test_atomesh_registers_a_worker_per_url_at_rank_of_get_server_info(topology, name):
    spec = next(router for router in ROUTERS if router.name == name)
    expected = set()
    for role, names in (("prefill", spec.prefill), ("decode", spec.decode)):
        for instance in map(topology.instances.get, names):
            size = json.loads(server_info(instance).server_info_json)["dp_size"]
            expected |= {(f"grpc://{instance.grpc}@{r}", role) for r in range(size)}
    status, listing = topology.routers[name].get("/workers")
    assert status == 200, listing
    workers = listing["workers"]
    assert {(worker["url"], worker["worker_type"]) for worker in workers} == expected
    found = {(worker["runtime_type"], worker["is_healthy"]) for worker in workers}
    assert found == {("atom", True)}


@pytest.mark.topology("dp")
@pytest.mark.parametrize("mode", ["relay", "chunked"])
@pytest.mark.parametrize("mapping", RANKS)
def test_each_request_runs_on_the_ranks_atomesh_picks_and_gets_what_http_gives(
    topology, tokenizer, mapping, mode
):
    p, d = topology.instances[f"p{mode}"], topology.instances["d"]
    paths = [topology.routers[f"{path}{mapping}_{mode}"] for path in ("http_", "")]
    lead = 10 * [*RANKS].index(mapping) + 5 * (mode == "chunked")
    for k, (session, ranks) in enumerate(zip(SESSIONS, RANKS[mapping])):
        prompt = words(8, lead + k)  # under one KV block: no run hits P's prefix cache
        for router in paths:
            wait_for(partial(idle, router), "an idle router")
            status, reply = complete(router, prompt, f"{mapping}-{mode}-{session}")
            assert status == 200, reply
        ids = tokenizer.encode(prompt)
        prefills, decodes = admitted(p, ids), admitted(d, ids)
        assert len(prefills) == len(decodes) == 2  # HTTP's, then gRPC's
        for prefill, decode in zip(prefills, decodes):
            assert (rank(prefill), rank(decode)) == ranks
            kv = decode.kv_transfer_params
            sent = kv["remote_dp_rank"], kv["remote_dp_size"], kv["remote_tp_size"]
            assert sent == (ranks[0], *SIZES[f"p{mode}"])
            source = prefill.kv_transfer_params.get("transfer_id", prefill.seq_id)
            assert kv["transfer_id"] == source  # D reads the P request that ran
        for http_path, grpc_path in (prefills, decodes):
            assert http_path.sampling == grpc_path.sampling
            http_kv = by_form(http_path.kv_transfer_params)
            assert http_kv == by_form(grpc_path.kv_transfer_params)


@pytest.mark.topology("dp")
@pytest.mark.parametrize("outcome", ["completion", "cancel", "error"])
def test_each_url_at_rank_load_returns_to_0_after_a_completion_cancel_or_error(
    topology, outcome
):
    router, d = topology.routers["cache"], topology.instances["d"]
    prompt = words(8, 30 + ["completion", "cancel", "error"].index(outcome))
    wait_for(partial(idle, router), "an idle router")
    if outcome == "error":
        status, reply = complete(router, prompt, top_p=0.0)
        assert status == 400, reply
    else:
        call = request(router, prompt, stream=True, max_tokens=500)
        address = urllib.parse.urlsplit(call.full_url)
        connection = http.client.HTTPConnection(address.hostname, address.port, 60)
        connection.request("POST", address.path, call.data, dict(call.header_items()))
        reply = connection.getresponse()
        assert reply.status == 200 and reply.readline().startswith(b"data: ")
        held = [url for url, load in loads(router).items() if load]
        assert len(held) == 1 and held[0].startswith(f"grpc://{d.grpc}@"), held
        if outcome == "cancel":
            connection.sock.shutdown(socket.SHUT_RDWR)
        else:
            assert b"data: [DONE]" in reply.read()
        connection.close()
    wait_for(partial(idle, router), "every url@rank load back to 0")


@pytest.mark.topology("dp")
def test_a_fault_on_one_d_rank_fails_only_the_requests_it_runs(topology, tokenizer):
    router, d = topology.routers["cache"], topology.instances["d"]
    faulted, prefix = FAULT["dp_rank"], tokenizer.decode(FAULT["prompt_prefix"])
    tokens = {}  # D rank: the completion tokens of each request it ran
    for lead in range(40, 100):  # each new prompt goes to an idle rank at random
        if faulted in tokens and len(tokens) > 1:
            break
        prompt = f"{prefix} {words(8, lead)}"
        status, reply = complete(router, prompt)
        assert status == 200, reply
        (request,) = admitted(d, tokenizer.encode(prompt))
        ran = request.data_parallel_rank != faulted
        assert (request.receive["ok"], bool(request.steps)) == (ran, ran)
        count = reply["usage"]["completion_tokens"]
        tokens.setdefault(request.data_parallel_rank, set()).add(count)
    assert tokens.pop(faulted, None) == {0}, tokens
    assert tokens and all(counts == {4} for counts in tokens.values()), tokens
    wait_for(partial(idle, router), "every url@rank load back to 0")


@pytest.mark.topology("dp")
def test_a_repeated_prefix_goes_back_to_the_ranks_that_served_it(topology, tokenizer):
    router = topology.routers["cache"]
    p, d = topology.instances["prelay"], topology.instances["d"]
    for lead in (60, 70, 80, 90):
        prefix, placed = words(8, lead), []
        for more in range(3):  # the prefix alone, then with one and two more words
            prompt = f"{prefix} {words(more, lead + 8)}".strip()
            wait_for(partial(idle, router), "equal loads")
            status, reply = complete(router, prompt)
            assert status == 200, reply
            ids = tokenizer.encode(prompt)
            (prefill,), (decode,) = admitted(p, ids), admitted(d, ids)
            placed.append((rank(prefill), rank(decode)))
        assert placed == placed[:1] * 3
