# SPDX-License-Identifier: MIT
"""The AgentX extract's turns as token ids over Generate, the Session and
`/v1/completions`: each ships the chain a full recompute gives, and hashes only the
complete blocks past those it shares with a stored prompt."""

import numpy as np
import pytest

from atom.model_engine.prefix_hash import chained_block_hashes
from tests.atom_grpc.a6.agentx import TokenBlocks, extract_turns
from tests.atom_grpc.a6.chains import TRANSPORTS, chain_counts, chain_verdict
from tests.atom_grpc.fake.record import RECORD, EngineRecord

BLOCK = 16  # the single topology's hash block: ATOM's default --block-size


def shared_blocks(prompts: list[list[int]]) -> list[int]:
    """Per prompt, the complete blocks it shares with the earlier one sharing most."""
    arrays, shared = [np.asarray(p) for p in prompts], []
    for i, p in enumerate(arrays):
        best = 0
        for q in arrays[:i]:
            n = min(len(p), len(q))
            diff = np.flatnonzero(p[:n] != q[:n])
            best = max(best, int(diff[0]) if len(diff) else n)
        shared.append(best // BLOCK)
    return shared


@pytest.mark.topology("single")
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_each_turn_ships_a_full_recompute_and_hashes_only_its_unshared_blocks(
    topology, transport
):
    (instance,) = topology.instances.values()
    seed = list(TRANSPORTS).index(transport)  # no block shared with another transport
    blocks = TokenBlocks(seed)
    prompts = [blocks.prompt(turn.hash_ids) for turn in extract_turns()]
    shared = shared_blocks(prompts)
    assert sum(s > 0 for s in shared) >= 20

    counts, before = [], chain_counts(instance)
    for _ in TRANSPORTS[transport](instance, prompts):
        after = chain_counts(instance)
        counts.append({key: after[key] - before[key] for key in after})
        before = after
    # Any stored prompt counts: a subagent group's first turn reuses another's start.
    assert counts == [
        {"prompts": 1, "blocks_hashed": len(p) // BLOCK - s, "blocks_reused": s}
        for p, s in zip(prompts, shared)
    ]
    record = EngineRecord(instance.workdir / RECORD)
    for prompt in prompts:
        (admitted,) = record.with_prompt(prompt)
        full = chained_block_hashes(prompt, BLOCK, 0, len(prompt) // BLOCK, -1)
        assert admitted.prompt_hash_chain == full
    adopted, rejected = chain_verdict(instance)
    assert adopted >= len(prompts) and rejected == 0
