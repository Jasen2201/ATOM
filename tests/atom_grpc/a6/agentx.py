# SPDX-License-Identifier: MIT
"""AgentX trace sessions as token-id conversations: a session's main turns form one,
each subagent group its own, and each hash id stands for a seeded block of 64 ids."""

import json
from pathlib import Path
from typing import NamedTuple

import numpy as np

# The local copy of semianalysisai/cc-traces-weka-062126 at revision 23f152f6.
TRACE = Path(
    "/share_nfs/models/datasets--semianalysisai--cc-traces-weka-062126/snapshots/"
    "23f152f6f0f9399a85901b89a6458def0ef16729/traces.jsonl"
)
EXTRACT = Path(__file__).resolve().parents[1] / "fixtures/agentx/extract.jsonl"
HASH_BLOCK = 64  # token ids per trace hash id
WORDS = (5, 108)  # the fixture tokenizer's word ids, no special one among them


class Turn(NamedTuple):
    conversation: str  # "main", or "subagent-<k>" for the session's k-th subagent group
    hash_ids: list[int]


def session_turns(session: dict) -> list[Turn]:
    """Every request of a trace session, in time order: a top-level one is a main turn,
    one inside a subagent group a turn of that group's conversation."""
    timed, groups = [], 0
    for request in session["requests"]:
        if request["type"] != "subagent":
            timed.append((request["t"], Turn("main", request["hash_ids"])))
            continue
        name, groups = f"subagent-{groups}", groups + 1
        timed += [(r["t"], Turn(name, r["hash_ids"])) for r in request["requests"]]
    return [turn for _, turn in sorted(timed, key=lambda pair: pair[0])]


def extract_turns(path: Path = EXTRACT) -> list[Turn]:
    """The committed extract's turns: each line's [start, stop) runs of hash ids."""
    turns = []
    for line in path.read_text().splitlines():
        cut = json.loads(line)
        ids = [i for start, stop in cut["hash_id_runs"] for i in range(start, stop)]
        turns.append(Turn(cut["conversation"], ids))
    return turns


class TokenBlocks:
    """Each hash id's block of token ids, drawn from a stream that `seed` and the id
    seed: fixed under one seed, and another block under any other."""

    def __init__(self, *seed: int):
        self.seed = seed
        self.blocks: dict[int, np.ndarray] = {}

    def prompt(self, hash_ids: list[int]) -> list[int]:
        for i in hash_ids:
            if i not in self.blocks:
                rng = np.random.default_rng([*self.seed, i])
                self.blocks[i] = rng.integers(*WORDS, HASH_BLOCK, dtype=np.int32)
        return np.concatenate([self.blocks[i] for i in hash_ids]).tolist()
