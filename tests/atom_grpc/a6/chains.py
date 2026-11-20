# SPDX-License-Identifier: MIT
"""Token-id prompts sent to a fake instance one at a time over each transport, and what
its frontend and its BlockManager report of the prompts' block-hash chains."""

import re

from tests.atom_grpc.test_grpc_generate import generate
from tests.atom_grpc.test_grpc_session import SessionCall, submit

COUNTS = ("prompts", "blocks_hashed", "blocks_reused")  # of `/debug/cache_stats`
GREEDY_ONE = {"max_tokens": 1, "temperature": 0}
LOGGED_EVERY = 100  # prefills from one of the engine's cache stats lines to the next
VERDICT = re.compile(
    r"\[Prompt Chains\] frontend-hashed: adopted (\d+), rejected (\d+)"
)


def _check(holds: bool, what) -> None:
    if not holds:
        raise RuntimeError(f"unexpected reply: {what}")


def over_generate(instance, prompts):
    """Each prompt as a unary Generate; yields once done."""
    for prompt in prompts:
        (message,) = generate(instance.grpc, prompt, **GREEDY_ONE)
        _check(message.WhichOneof("response") == "complete", message)
        yield


def over_session(instance, prompts):
    """Each prompt as one request of one Session; yields once done."""
    with SessionCall(instance.grpc) as session:
        for seq_id, prompt in enumerate(prompts):
            session.send(submit({seq_id: (prompt, GREEDY_ONE)}))
            ended = session.until_ended([seq_id])[seq_id]
            _check(ended.leave_reason == "max_tokens", ended)
            yield


def over_completions(instance, prompts):
    """Each prompt as a `/v1/completions` body's prompt_token_ids; yields once done."""
    for prompt in prompts:
        body = {"prompt_token_ids": prompt, **GREEDY_ONE}
        status, reply = instance.post("/v1/completions", body)
        _check(status == 200, reply)
        yield


TRANSPORTS = {
    "generate": over_generate,
    "session": over_session,
    "completions": over_completions,
}


def cache_stats(instance) -> dict:
    status, stats = instance.get("/debug/cache_stats")
    _check(status == 200, stats)
    return stats


def chain_counts(instance) -> dict:
    """The frontend's token-id prompts and their complete blocks hashed and reused."""
    stats = cache_stats(instance)["token_prefix_chains"]
    return {key: stats[key] for key in COUNTS}


def chain_verdict(instance) -> tuple[int, int]:
    """BlockManager's shipped chains adopted and rejected, from the line its engine logs
    every 100th prefill; one-token prefills first take the count to the next 100th."""
    for _ in range(-cache_stats(instance)["requests"] % LOGGED_EVERY):
        body = {"prompt_token_ids": [5], **GREEDY_ONE}
        status, reply = instance.post("/v1/completions", body)
        _check(status == 200, reply)
    found = VERDICT.findall((instance.workdir / "server.log").read_text())
    _check(bool(found), "no [Prompt Chains] line in the instance's log")
    adopted, rejected = found[-1]
    return int(adopted), int(rejected)
