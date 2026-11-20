# SPDX-License-Identifier: MIT
"""Fake Mooncake on the 1p1d topology, P and D driven over HTTP as atomesh's AtomAdapter
drives them: the KV a transfer moves and when, its faults, and D's receive hold."""

import concurrent.futures
import http.client
import json
import time
import urllib.parse
import uuid
from pathlib import Path

import pytest
import tomllib
from transformers import AutoTokenizer

from tests.atom_grpc.fake.ids import IdLaw, fingerprint
from tests.atom_grpc.fake.record import RECORD, EngineRecord
from tests.atom_grpc.harness.topologies import TOPOLOGIES

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
P_FAKE, D_FAKE = (
    instance["fake"]
    for instance in tomllib.loads((TOPOLOGIES / "1p1d.toml").read_text())["instance"]
)
HOLD_S = float(D_FAKE["env"]["ATOM_PD_FAILED_WRITE_HOLD_S"])
WAIT_S = 120  # for a /metrics gauge to change on a loaded host


@pytest.fixture(scope="module")
def tokenizer():
    return AutoTokenizer.from_pretrained(FIXTURE)


def law_text(tokenizer, prompt: list[int], count: int) -> str:
    """What one instance answers: the text of the law's first `count` ids for `prompt`."""
    law = IdLaw.for_model(FIXTURE, {tokenizer.eos_token_id})
    return tokenizer.decode(law.ids(fingerprint(prompt), 0, count))


def completion(prompt: list[int], max_tokens: int) -> dict:
    return {"prompt_token_ids": prompt, "max_tokens": max_tokens, "temperature": 0}


class PD:
    """The topology's P and D, sent a request as `AtomAdapter` and atomesh's DP-aware
    HTTP P/D router send it: D on DP rank `rank`, `fault` tagging D's params."""

    P_RANK = 0  # P's one DP rank

    def __init__(self, topology):
        self.p, self.d = topology.instances["p"], topology.instances["d"]
        self.info = self.p.get("/kv_transfer_info")[1]

    def prefill(self, body: dict, transfer_id: str | None = None) -> dict:
        kv = {"do_remote_decode": True, "do_remote_prefill": False}
        if transfer_id is not None:
            kv |= {"chunked_transfer": True, "transfer_id": transfer_id}
        body = body | {"stream": False, "max_tokens": 1, "return_token_ids": True}
        return body | {"kv_transfer_params": kv, "data_parallel_rank": self.P_RANK}

    def decode(self, body: dict, kv: dict, fault: str | None, rank: int) -> dict:
        kv = kv | {"remote_dp_size": self.info["dp_size"]}
        kv["remote_tp_size"] = self.info["tp_size"]
        kv["remote_dp_rank"] = kv.get("dp_rank", self.P_RANK)
        if fault is not None:
            kv["fake_fault"] = fault
        return body | {"kv_transfer_params": kv, "data_parallel_rank": rank}

    def relayed(self, body: dict, fault: str | None = None, rank: int = 0) -> dict:
        """D's body, once P answered the prefill."""
        status, reply = self.p.post("/v1/completions", self.prefill(body))
        assert status == 200, reply
        kv = reply["kv_transfer_params"]
        kv["prompt_token_ids"] = reply["prompt_token_ids"]
        return self.decode(body, kv, fault, rank)

    def relay(
        self, body: dict, fault: str | None = None, rank: int = 0
    ) -> tuple[int, dict]:
        return self.d.post("/v1/completions", self.relayed(body, fault, rank))

    def chunked(
        self, body: dict, fault: str | None = None, rank: int = 0
    ) -> tuple[int, dict]:
        transfer_id = f"xfer-{uuid.uuid4()}"
        kv = self.info["chunked_transfer"] | {"chunked_transfer": True}
        kv |= {"do_remote_prefill": True, "do_remote_decode": False}
        kv |= {"transfer_id": transfer_id}
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            prefill = self.prefill(body, transfer_id)
            p = pool.submit(self.p.post, "/v1/completions", prefill)
            decode = self.decode(body, kv, fault, rank)
            d = pool.submit(self.d.post, "/v1/completions", decode)
            assert p.result()[0] == 200, p.result()
            return d.result()

    @staticmethod
    def record(instance, prompt: list[int]) -> list:
        return EngineRecord(instance.workdir / RECORD).with_prompt(prompt)


def gauges(instance) -> dict[str, float]:
    """The instance's Prometheus gauges, as its engines last pushed them."""
    url = urllib.parse.urlsplit(instance.url)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=60)
    conn.request("GET", "/metrics")
    lines = conn.getresponse().read().decode().splitlines()
    conn.close()
    return {n: float(v) for n, v, *_ in (s.split() for s in lines if s[:5] == "atom:")}


def read_when(instance, gauge: str, value: float) -> float:
    """The wall time of the first read of `gauge` that gives `value`."""
    deadline = time.monotonic() + WAIT_S
    while gauges(instance).get(gauge) != value:
        assert time.monotonic() < deadline, f"{gauge} never read {value}"
        time.sleep(0.2)
    return time.time()


@pytest.mark.topology("1p1d")
@pytest.mark.parametrize("mode", ["relay", "chunked"])
def test_a_transfer_moves_the_prompt_kv_and_lasts_its_bytes_over_the_bandwidth(
    topology, tokenizer, mode
):
    pd = PD(topology)
    prompt = [1, 40 + len(mode), *range(10, 48)]  # two full blocks and part of a third
    status, reply = getattr(pd, mode)(completion(prompt, 12))
    assert status == 200, reply
    choice = reply["choices"][0]
    assert choice["finish_reason"] == "length"
    assert choice["text"] == law_text(tokenizer, prompt, 12)  # T0 from P, then D's

    (request,), (prefill,) = pd.record(pd.d, prompt), pd.record(pd.p, prompt)
    receive = request.receive
    assert receive["ok"] and receive["blocks"] == 3
    assert receive["bytes"] == 3 * P_FAKE["block_bytes"]
    duration = receive["bytes"] / P_FAKE["bandwidth_gb_s"] / 1e9
    assert receive["seconds"] == pytest.approx(duration)
    assert prefill.steps[0].time <= receive["start"]  # P's KV was there to send
    assert receive["end"] <= request.steps[0].time  # D decodes once it has arrived
    if mode == "relay":  # Mooncake's relay transfer id is P's request id
        assert receive["transfer_id"] == prefill.seq_id
    else:
        assert receive["transfer_id"] == request.kv_transfer_params["transfer_id"]


@pytest.mark.topology("1p1d")
def test_a_receive_moves_only_the_blocks_d_does_not_hold(topology, tokenizer):
    pd = PD(topology)
    first = [1, 7, *range(20, 58)]  # two full blocks and part of a third
    second = [*first[:32], *range(60, 80)]  # the same two full blocks, then two more
    for prompt in (first, second):
        status, reply = pd.relay(completion(prompt, 6))
        assert status == 200, reply
        assert reply["choices"][0]["text"] == law_text(tokenizer, prompt, 6)
    (request,) = pd.record(pd.d, second)
    assert request.receive["blocks"] == 2
    assert request.receive["bytes"] == 2 * P_FAKE["block_bytes"]


@pytest.mark.topology("1p1d")
@pytest.mark.parametrize("mode", ["relay", "chunked"])
@pytest.mark.parametrize("fault", ["fail", "timeout"])
def test_a_tagged_transfer_fails_and_d_reports_it_a_hold_later(topology, mode, fault):
    pd = PD(topology)
    prompt = [1, 50 + len(mode), 60 + len(fault), *range(30, 50)]  # two blocks
    status, reply = getattr(pd, mode)(completion(prompt, 8), fault)
    assert status == 200, reply
    assert reply["usage"]["completion_tokens"] == 0  # D aborted the request
    assert reply["choices"][0]["finish_reason"] == "stop"

    (request,) = pd.record(pd.d, prompt)
    receive = request.receive
    assert not receive["ok"] and not request.steps
    assert receive["bytes"] == 2 * P_FAKE["block_bytes"]
    failed_after = P_FAKE["transfer_timeout_s"] if fault == "timeout" else 0.0
    assert receive["seconds"] == pytest.approx(failed_after)
    assert receive["time"] >= receive["end"] + HOLD_S


@pytest.mark.topology("1p1d")
def test_a_rule_naming_one_dp_rank_fails_only_that_ranks_receives(topology, tokenizer):
    (rule,) = [rule for rule in D_FAKE["faults"] if "dp_rank" in rule]
    other, ruled = 1 - rule["dp_rank"], rule["dp_rank"]  # D's two DP ranks
    pd = PD(topology)
    prompt = [*rule["prompt_prefix"], *range(70, 90)]
    texts = {}
    for rank in (other, ruled):
        status, reply = pd.relay(completion(prompt, 8), rank=rank)
        assert status == 200, reply
        texts[rank] = reply["choices"][0]["text"]
    assert texts == {other: law_text(tokenizer, prompt, 8), ruled: ""}
    outcomes = {r.data_parallel_rank: r.receive["ok"] for r in pd.record(pd.d, prompt)}
    assert outcomes == {other: True, ruled: False}


@pytest.mark.topology("1p1d")
def test_a_request_aborted_while_its_receive_waits_keeps_its_blocks_till_it_reports(
    topology,
):
    pd = PD(topology)
    prompt = [1, 9, 9, *range(90, 110)]
    body = json.dumps(pd.relayed(completion(prompt, 8), "timeout"))
    url = urllib.parse.urlsplit(pd.d.url)
    client = http.client.HTTPConnection(url.hostname, url.port)
    client.request(
        "POST", "/v1/completions", body, {"Content-Type": "application/json"}
    )
    read_when(pd.d, "atom:requests_parked_kv_load", 1.0)
    client.close()  # the client goes away while D waits for the KV: D aborts
    held = read_when(pd.d, "atom:requests_deferred_free", 1.0)
    freed = read_when(pd.d, "atom:requests_deferred_free", 0.0)

    (request,) = pd.record(pd.d, prompt)
    receive = request.receive
    assert not receive["ok"]
    timeout = P_FAKE["transfer_timeout_s"]
    assert receive["seconds"] == pytest.approx(timeout)
    assert held < receive["time"] <= freed  # held while it waited, freed once reported
