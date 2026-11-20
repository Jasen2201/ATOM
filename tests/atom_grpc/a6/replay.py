# SPDX-License-Identifier: MIT
"""`python -m tests.atom_grpc.a6.replay [--trace traces.jsonl]`: the AgentX trace's
first five sessions through one fake instance, turn by turn, over Generate, then over
the Session; prints each pass's reuse rate and the chains BlockManager rejected."""

import argparse
import itertools
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from tests.atom_grpc.a6.agentx import TRACE, TokenBlocks, session_turns
from tests.atom_grpc.a6.chains import TRANSPORTS, chain_counts, chain_verdict
from tests.atom_grpc.fake.instance import FakeInstance, free_port
from tests.atom_grpc.harness.launch import STARTUP_S

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/deepseek_v3_mtp"
CONTEXT = 1 << 20  # tokens, past the trace's cap of 990,016 on a request
BLOCK = 16  # ATOM's default --block-size
ARGS = ["--method", "mtp", "--num-speculative-tokens", "3"]  # the single topology's
FAKE = {
    "acceptance_length": 3.51,
    "step_s": 0.001,
    "num_kvcache_blocks": CONTEXT // BLOCK,
}
PASSES = ("generate", "session")  # each with token blocks of its own


class GrpcInstance(FakeInstance):
    """A fake instance with no engine record, serving gRPC at `grpc`."""

    def __init__(self, workdir: Path, model: Path):
        port = free_port()
        args = [*ARGS, "--grpc-port", str(port)]
        super().__init__(workdir, model, args, FAKE, record=False)
        self.grpc = f"127.0.0.1:{port}"


def widened(scratch: Path) -> Path:
    """The fixture model with a context that holds every trace request."""
    model = scratch / "model"
    shutil.copytree(FIXTURE, model)
    config = json.loads((model / "config.json").read_text())
    config["max_position_embeddings"] = CONTEXT
    (model / "config.json").write_text(json.dumps(config))
    return model


def replay(instance, sessions: list[dict], transport: str, seed: int) -> None:
    before = chain_counts(instance)
    for index, session in enumerate(sessions):
        blocks = TokenBlocks(seed, index)  # hash ids are each session's own
        prompts = (blocks.prompt(turn.hash_ids) for turn in session_turns(session))
        for _ in TRANSPORTS[transport](instance, prompts):
            pass
    after = chain_counts(instance)
    hashed, reused = (after[k] - before[k] for k in ("blocks_hashed", "blocks_reused"))
    print(
        f"{transport}: {after['prompts'] - before['prompts']} prompts, "
        f"{hashed + reused} complete blocks, {reused} reused, {hashed} hashed: "
        f"reuse rate {reused / (hashed + reused):.2%}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, default=TRACE)
    parser.add_argument("--sessions", type=int, default=5)
    args = parser.parse_args()
    with open(args.trace) as f:
        sessions = [json.loads(line) for line in itertools.islice(f, args.sessions)]
    scratch = Path(tempfile.mkdtemp(prefix="a6-"))  # short: ZMQ ipc sockets live in it
    (scratch / "instance").mkdir()
    instance = GrpcInstance(scratch / "instance", widened(scratch))
    print(f"instance log: {instance.workdir / 'server.log'}", flush=True)
    try:
        instance.start()
        instance.wait_ready(time.monotonic() + STARTUP_S)
        for seed, transport in enumerate(PASSES):
            replay(instance, sessions, transport, seed)
        adopted, rejected = chain_verdict(instance)
    finally:
        instance.stop()
    print(f"BlockManager: adopted {adopted} shipped chains, rejected {rejected}")
    if rejected:
        return 1
    shutil.rmtree(scratch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
