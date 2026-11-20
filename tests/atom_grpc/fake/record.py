# SPDX-License-Identifier: MIT
"""The engine record: what a fake instance's engine was given and produced, as JSONL
lines its processes append to the file `RECORD_ENV` names. `EngineRecord` reads it."""

import json
import os
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import NamedTuple

from tests.atom_grpc.fake.serve import RECORD_ENV

RECORD = "engine_record.jsonl"  # its name in an instance's workdir
# SamplingParams' fields as a Sequence holds them (`logprobs` as `return_logprobs`).
SAMPLING = (
    "temperature",
    "top_k",
    "top_p",
    "max_tokens",
    "ignore_eos",
    "stop_strings",
    "top_logprobs",
    "required_channel",
    "json_schema",
    "structured_tool_format",
    "structured_reasoning_end",
)
# Every encode and decode entry point of a `tokenizers.Tokenizer`.
CALLS = (
    "encode",
    "encode_batch",
    "encode_batch_fast",
    "async_encode",
    "async_encode_batch",
    "async_encode_batch_fast",
    "decode",
    "decode_batch",
    "async_decode_batch",
)


def _plain(value):
    """What `json` cannot write as JSON data: id arrays, numpy values, dataclasses."""
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "tolist"):
        return value.tolist()
    raise TypeError(f"{type(value).__name__} is not JSON data")


def _lines(events) -> bytes:
    return "".join(json.dumps(e, default=_plain) + "\n" for e in events).encode()


def _admission(seq, arrival: float) -> dict:
    sampling = {name: getattr(seq, name) for name in SAMPLING}
    return {
        "kind": "admit",
        "time": arrival,
        "seq_id": seq.id,
        "request_id": seq.external_request_id,
        "prompt_ids": seq.token_ids[: seq.num_prompt_tokens],
        "sampling": sampling | {"logprobs": seq.return_logprobs},
        "stop_token_sequences": seq.stop_token_sequences,
        "kv_transfer_params": seq.kv_transfer_params,
        "data_parallel_rank": seq.data_parallel_rank,
    }


class Recorder:
    """Appends to one instance's record, one write per call, so that the lines of its
    processes interleave whole."""

    def __init__(self, path: str):
        self.fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)

    @classmethod
    def from_env(cls) -> "Recorder | None":
        path = os.environ.get(RECORD_ENV)
        return cls(path) if path else None

    def write(self, lines: bytes) -> None:
        os.write(self.fd, lines)

    def step(self, dp_rank: int, rows: list, end: float) -> None:
        """One step's ids, `rows` as (seq_id, ids), and the wall-clock time it ended."""
        event = {"kind": "step", "time": end, "dp_rank": dp_rank, "rows": rows}
        self.write(_lines([event]))

    def watch(self, tokenizer):
        """Record every encode and decode of `tokenizer`'s backend; returns it."""
        backend = tokenizer._tokenizer
        for name in CALLS:
            if hasattr(backend, name):
                setattr(backend, name, self._recorded(name, getattr(backend, name)))
        return tokenizer

    def _recorded(self, name: str, method):
        kind = "encode" if "encode" in name else "decode"
        batch = "batch" in name

        def call(inputs, *args, **kwargs):
            event = {"kind": kind, "time": time.time(), "inputs": inputs}
            if not batch:
                event["inputs"] = [inputs]
            self.write(_lines([event]))
            return method(inputs, *args, **kwargs)

        return call


def record_api_process() -> None:
    """When `RECORD_ENV` is set, record what this API process admits to its engine and
    every tokenizer call it makes."""
    recorder = Recorder.from_env()
    if recorder is None:
        return
    from atom.entrypoints.openai import api_server
    from atom.model_engine.engine_core_mgr import CoreManager

    add_request, load_tokenizer = CoreManager.add_request, api_server.load_tokenizer

    def recorded_add_request(manager, seqs):
        now = time.time()
        lines = _lines([_admission(seq, now) for seq in seqs])  # as given
        add_request(manager, seqs)
        recorder.write(lines)

    def recorded_tokenizer(*args, **kwargs):
        return recorder.watch(load_tokenizer(*args, **kwargs))

    CoreManager.add_request = recorded_add_request
    api_server.load_tokenizer = recorded_tokenizer


class Step(NamedTuple):
    time: float  # when the fake GPU ended it, on the wall clock
    dp_rank: int
    ids: list[int]


@dataclass
class Request:
    """One admitted sequence as the record holds it, with its steps in record order."""

    seq_id: int
    request_id: str | None
    time: float  # its arrival at `CoreManager.add_request`
    prompt_ids: list[int]
    sampling: dict
    stop_token_sequences: list[list[int]]
    kv_transfer_params: dict | None
    data_parallel_rank: int | None
    steps: list[Step] = field(default_factory=list)

    @property
    def output_ids(self) -> list[int]:
        """Every id its steps carry; those of a step past its finish reach no client."""
        return [token for step in self.steps for token in step.ids]


class EngineRecord:
    """A record read back: `requests` by seq id, and the API process's `tokenizer`
    calls in order, each {kind: encode or decode, time, inputs}."""

    def __init__(self, path: Path):
        text = path.read_text()
        whole = text[: text.rfind("\n") + 1]  # a process may be mid-write
        events = [json.loads(line) for line in whole.splitlines()]
        steps = defaultdict(list)
        for event in events:
            if event["kind"] == "step":
                for seq_id, ids in event["rows"]:
                    steps[seq_id].append(Step(event["time"], event["dp_rank"], ids))
        self.requests: dict[int, Request] = {}
        self.tokenizer: list[dict] = []
        for event in events:
            if event["kind"] == "admit":
                fields = {k: v for k, v in event.items() if k != "kind"}
                self.requests[event["seq_id"]] = Request(
                    **fields, steps=steps[event["seq_id"]]
                )
            elif event["kind"] != "step":
                self.tokenizer.append(event)

    def with_prompt(self, prompt_ids: list[int]) -> list[Request]:
        """The sequences admitted with `prompt_ids`, in admission order."""
        return [r for r in self.requests.values() if r.prompt_ids == prompt_ids]
