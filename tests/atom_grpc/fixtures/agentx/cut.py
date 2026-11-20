# SPDX-License-Identifier: MIT
"""`python -m tests.atom_grpc.fixtures.agentx.cut [traces.jsonl]` writes extract.jsonl:
the first TURNS requests of the AgentX trace's fifth session, in time order, one line a
turn with its conversation and its hash ids as [start, stop) runs."""

import itertools
import json
import sys
from pathlib import Path

from tests.atom_grpc.a6.agentx import EXTRACT, TRACE, session_turns

SESSION = 4  # 02bc0afb13f7: six main turns, then two subagent groups sharing a start
TURNS = 24


def runs(ids: list[int]) -> list[list[int]]:
    """`ids` as [start, stop) runs of consecutive ids."""
    out = []
    for i in ids:
        if out and out[-1][1] == i:
            out[-1][1] += 1
        else:
            out.append([i, i + 1])
    return out


def main() -> None:
    trace = Path(sys.argv[1]) if len(sys.argv) > 1 else TRACE
    with open(trace) as f:
        session = json.loads(next(itertools.islice(f, SESSION, None)))
    lines = (
        json.dumps({"conversation": t.conversation, "hash_id_runs": runs(t.hash_ids)})
        for t in session_turns(session)[:TURNS]
    )
    EXTRACT.write_text("".join(line + "\n" for line in lines))


if __name__ == "__main__":
    main()
