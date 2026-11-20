# SPDX-License-Identifier: MIT
"""Fake instances on CPU: ATOM's own API server, CoreManager, EngineCore, Scheduler
and BlockManager over `FakeRunner`, with no aiter and no triton installed."""

import sys
import time
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest
from transformers import AutoTokenizer, PretrainedConfig

from atom.config import SpeculativeConfig
from atom.entrypoints.openai.tool_parser.kimi_k3_tool_parser import KimiK3Parser
from atom.entrypoints.openai.tool_parser.registry import required_call_channel
from atom.model_engine.prefix_hash import prompt_fingerprint
from tests.atom_grpc.fake.ids import stride
from tests.atom_grpc.fake.instance import FakeInstance

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
LENGTH = 3.51  # the deployment recipe's mean tokens per verify step
STARTUP_S = 1500  # one deadline for both; a loaded host takes minutes per instance
INSTANCES = {
    "mtp": ["--method", "mtp", "--num-speculative-tokens", "3"],
    "dp2": ["-tp", "2", "--enable-dp-attention"],
}


@pytest.fixture(scope="module")
def instances(tmp_path_factory):
    fake = {"acceptance_length": LENGTH, "step_s": 0.001}
    started = {
        name: FakeInstance(tmp_path_factory.mktemp(name), FIXTURE, args, fake)
        for name, args in INSTANCES.items()
    }
    try:
        for instance in started.values():
            instance.start()
        deadline = time.monotonic() + STARTUP_S
        for instance in started.values():
            instance.wait_ready(deadline)
        yield started
    finally:
        for instance in started.values():
            instance.stop()


@pytest.fixture(scope="module")
def tokenizer():
    return AutoTokenizer.from_pretrained(FIXTURE)


def law_text(tokenizer, prompt: list[int], count: int) -> str:
    """The text of the law's first `count` ids for `prompt`: F[(h + p * s) mod |F|]."""
    fill = []
    for token_id in range(len(tokenizer)):
        text = tokenizer.decode([token_id])
        if token_id != tokenizer.eos_token_id and text.isascii() and text.isalnum():
            fill.append(token_id)
    seq = SimpleNamespace(token_ids=array("i", prompt), num_prompt_tokens=len(prompt))
    h, s = prompt_fingerprint(seq), stride(len(fill))
    ids = [fill[(h + p * s) % len(fill)] for p in range(count)]
    return tokenizer.decode(ids, skip_special_tokens=True)


def complete(instance, prompt: list[int], max_tokens: int, **extra) -> dict:
    body = {"prompt_token_ids": prompt, "max_tokens": max_tokens, "temperature": 0}
    status, reply = instance.post("/v1/completions", {**body, **extra})
    assert status == 200, reply
    assert reply["usage"]["completion_tokens"] == max_tokens
    return reply["choices"][0]


def spec_counts(instance) -> tuple[int, int]:
    """ATOM's own MTP tally: verify steps, and the drafts they accepted."""
    status, stats = instance.get("/debug/mtp_stats")
    assert status == 200, stats
    tally = {int(accepted): steps for accepted, steps in stats["distribution"].items()}
    return sum(tally.values()), sum(a * steps for a, steps in tally.items())


def cached_tokens(instance) -> int:
    status, stats = instance.get("/debug/cache_stats")
    assert status == 200, stats
    return stats["cached_tokens"]


def test_only_forced_acceptance_imports_the_triton_backed_rejection_sampler(
    monkeypatch,
):
    monkeypatch.setitem(sys.modules, "atom.model_ops.rejection_sampler", None)

    def mtp(**forced):
        hf = PretrainedConfig(model_type="deepseek_v3", num_nextn_predict_layers=1)
        return SpeculativeConfig(
            method="mtp", num_speculative_tokens=3, draft_model_hf_config=hf, **forced
        )

    assert mtp().synthetic_acceptance_rates is None
    with pytest.raises(ImportError, match="rejection_sampler"):
        mtp(synthetic_acceptance_length=3.0)


def test_the_fixture_tokenizer_holds_the_kimi_k3_required_channel(tokenizer):
    channel = required_call_channel(KimiK3Parser, tokenizer)
    assert channel is not None
    ids = (*channel.after, *channel.opening, *channel.end_ids)
    assert tokenizer.unk_token_id not in ids


def test_an_mtp_instance_emits_the_law_ids_through_accepted_and_rejected_drafts(
    instances, tokenizer
):
    prompt = [1, *range(8, 45)]  # two full blocks and part of a third
    choice = complete(instances["mtp"], prompt, 50)
    assert choice["finish_reason"] == "length"
    assert choice["text"] == law_text(tokenizer, prompt, 50)


def test_the_fixture_chat_template_serves_a_chat_completion(instances, tokenizer):
    body = {"messages": [{"role": "user", "content": "ba be bi"}], "max_tokens": 16}
    body |= {"temperature": 0, "return_token_ids": True}
    status, reply = instances["mtp"].post("/v1/chat/completions", body)
    assert status == 200, reply
    choice, prompt = reply["choices"][0], reply["prompt_token_ids"]
    assert prompt == [1, 3, 8, 9, 10, 4]  # <s><|user|>ba be bi<|assistant|>
    assert choice["finish_reason"] == "length"
    assert choice["message"]["content"] == law_text(tokenizer, prompt, 16)


@pytest.mark.parametrize("rank", [0, 1])
def test_each_rank_of_a_dp2_instance_emits_the_law_ids(instances, tokenizer, rank):
    prompt = [1, 60 + rank, 61, 62]
    choice = complete(instances["dp2"], prompt, 20, data_parallel_rank=rank)
    assert choice["finish_reason"] == "length"
    assert choice["text"] == law_text(tokenizer, prompt, 20)


def test_a_repeated_prompt_hits_the_prefix_cache_and_keeps_its_ids(
    instances, tokenizer
):
    mtp = instances["mtp"]
    prompt = [1, *(30 + i % 70 for i in range(79))]  # five 16-token blocks
    before = cached_tokens(mtp)
    first = complete(mtp, prompt, 24)
    between = cached_tokens(mtp)
    second = complete(mtp, prompt, 24)
    assert between == before
    assert cached_tokens(mtp) > between
    assert first["text"] == second["text"] == law_text(tokenizer, prompt, 24)


def test_mtp_steps_average_the_configured_acceptance_length(instances):
    mtp = instances["mtp"]
    steps_before, accepted_before = spec_counts(mtp)
    complete(mtp, [1, 8, 9], 3600)
    steps, accepted = spec_counts(mtp)
    steps, accepted = steps - steps_before, accepted - accepted_before
    assert steps >= 1000
    assert abs(1 + accepted / steps - LENGTH) <= 0.01 * LENGTH
