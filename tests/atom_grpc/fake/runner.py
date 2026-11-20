# SPDX-License-Identifier: MIT
"""Fake ModelRunner: answers every RPC EngineCore waits on, on CPU, with no model.
Steps run back to back on one fake GPU at a constant price; ids follow `IdLaw`."""

import json
import math
import os
import time
from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from atom.model_engine.scheduler import ScheduledBatchOutput, defers_output
from atom.model_engine.state_runtime import StateRuntime
from tests.atom_grpc.fake.ids import IdLaw, fingerprint

CONFIG_ENV = "FAKE_ENGINE_CONFIG"


@dataclass(frozen=True)
class FakeConfig:
    """A fake instance's knobs: the JSON file `FAKE_ENGINE_CONFIG` names, else these."""

    acceptance_length: float = 3.51  # mean tokens per verify step
    step_s: float = 0.001  # the constant-step law: the price of every step
    num_kvcache_blocks: int = 4096

    @classmethod
    def from_env(cls) -> "FakeConfig":
        path = os.environ.get(CONFIG_ENV)
        if not path:
            return cls()
        with open(path) as f:
            return cls(**json.load(f))


def verify_tokens(step: int, length: Fraction) -> int:
    """Tokens a request's `step`-th verify emits; its first n emit floor(n * length)."""
    return math.floor((step + 1) * length) - math.floor(step * length)


class SerialGpu:
    """One fake GPU: a step starts once it is called and the step before it ends."""

    def __init__(self):
        self.free_at = 0.0

    def run(self, price: float) -> float:
        """Queue a step of `price` seconds; returns when it ends (monotonic)."""
        self.free_at = max(time.monotonic(), self.free_at) + price
        return self.free_at


def _sleep_until(t: float) -> None:
    delay = t - time.monotonic()
    if delay > 0:
        time.sleep(delay)


@dataclass
class _Request:
    fingerprint: int
    prompt_len: int
    verify_steps: int = 0
    rejected: int = 0  # drafts the last verify rejected; 0 after a prefill


class FakeRunner:
    """ModelRunner's stand-in, named by `runner_qualname`: one per TP rank process."""

    def __init__(self, rank: int, config):
        self.rank = rank
        self.fake = FakeConfig.from_env()
        spec = config.speculative_config
        self.mtp_k = spec.num_speculative_tokens if spec is not None else 0
        self.length = Fraction(str(self.fake.acceptance_length))
        if self.mtp_k and not 1 <= self.length <= self.mtp_k + 1:
            raise ValueError(
                f"acceptance length {self.length} is outside [1, {self.mtp_k + 1}]"
            )
        self.deferred = defers_output(config)
        self.block_size = config.kv_cache_block_size
        stop = {config.eos_token_id, *config.stop_token_ids} - {None}
        self.law = IdLaw.for_model(config.model, stop)
        self.gpu = SerialGpu()
        self.kv = np.zeros(0, np.int32)  # the token id each KV slot holds
        self.requests: dict[int, _Request] = {}
        self.pending = None  # deferred output: the last output step's (output, end)

    def get_num_blocks(self) -> dict:
        return {
            "num_kvcache_blocks": self.fake.num_kvcache_blocks,
            "pool_entries": {},
            "pool_entry_bytes": {},
            "pool_entries_per_req": {},
            "state_runtime": StateRuntime().to_wire(),
        }

    def allocate_kv_cache(self, num_kvcache_blocks: int) -> bool:
        self.kv = np.zeros(num_kvcache_blocks * self.block_size, np.int32)
        return True

    def capture_cudagraph(self):
        return 0.0, [], 0

    def prefill_token_graphs(self) -> bool:
        return False

    def freeze_gc_heap(self) -> int:
        return 0

    def release_constraints(self, request_ids) -> None:
        pass

    def release_multimodal_requests(self, request_ids) -> None:
        pass

    def exit(self) -> None:
        pass

    def dummy_execution(self) -> ScheduledBatchOutput:
        end = self.gpu.run(self.fake.step_s)
        return self._hand_back(self._output([], [], [], []), end)

    def forward(self, batch) -> ScheduledBatchOutput:
        end = self.gpu.run(self.fake.step_s)
        output = self._step(batch)
        if not batch.produces_output():
            return ScheduledBatchOutput(list(batch.req_ids), [], None, None, None)
        return self._hand_back(output, end)

    def _hand_back(self, output, end: float) -> ScheduledBatchOutput:
        """The output a step returns once it is ready: deferred, the step before's."""
        if self.deferred:
            empty = (self._output([], [], [], []), 0.0)
            self.pending, (output, end) = (output, end), self.pending or empty
        _sleep_until(end)
        return output

    def _step(self, batch) -> ScheduledBatchOutput:
        """Write the step's KV, then emit each row's ids at their true positions."""
        k = batch.num_spec_step
        token_ends = np.cumsum(batch.num_scheduled_tokens)
        rows, drafts, rejected = [], [], []
        for i, req_id in enumerate(batch.req_ids):
            table, end = batch.block_tables[i], int(batch.context_lens[i])
            if i < batch.total_seqs_num_prefill:
                start, stop = int(batch.num_cached_tokens[i]), int(token_ends[i])
                self._write(
                    table, start, batch.scheduled_tokens[stop - end + start : stop]
                )
                if batch.is_final_chunk is not None and not batch.is_final_chunk[i]:
                    # A middle chunk samples too, and the scheduler drops it.
                    rows.append((self.law.fill[0],))
                    drafts.append([self.law.fill[0]] * self.mtp_k)
                    rejected.append(0)
                    continue
                req, first, count, reject = self._admit(req_id, table, end), end, 1, 0
            else:
                req, first, count, reject = self._known(req_id), end, 1, 0
                if k:
                    # The window ends where ModelRunner's `verify_ctx_lens` puts it.
                    first = end - k - (req.rejected if self.deferred else 1)
                    count = verify_tokens(req.verify_steps, self.length)
                    reject = req.rejected = k + 1 - count
                    req.verify_steps += 1
                self._write(table, first - 1, self._ids(req, first - 1, first + k))
            ids = self._ids(req, first, first + count + self.mtp_k)
            rows.append(tuple(ids[:count]))
            drafts.append(ids[count:])
            rejected.append(reject)
        return self._output(list(batch.req_ids), rows, drafts, rejected)

    def _admit(self, req_id: int, table, end: int) -> _Request:
        """A final prefill chunk: a new request's prompt is what its KV slots hold."""
        req = self.requests.get(req_id)
        if req is None:
            prompt = self.kv[self._slots(table, 0, end)].tolist()
            req = self.requests[req_id] = _Request(fingerprint(prompt), end)
        req.rejected = 0
        return req

    def _known(self, req_id: int) -> _Request:
        req = self.requests.get(req_id)
        if req is None:
            raise RuntimeError(f"request {req_id} decodes with no prefill seen here")
        return req

    def _ids(self, req: _Request, start: int, stop: int) -> list[int]:
        return self.law.ids(req.fingerprint, start - req.prompt_len, stop - start)

    def _slots(self, table, start: int, stop: int) -> np.ndarray:
        positions = np.arange(start, stop)
        blocks = np.asarray(table, np.int64)[positions // self.block_size]
        return blocks * self.block_size + positions % self.block_size

    def _write(self, table, start: int, tokens) -> None:
        self.kv[self._slots(table, start, start + len(tokens))] = tokens

    def _output(self, req_ids, rows, drafts, rejected) -> ScheduledBatchOutput:
        return ScheduledBatchOutput(
            req_ids,
            rows,
            num_rejected=np.asarray(rejected, np.int32),
            num_bonus=np.asarray([len(row) - 1 for row in rows], np.int32),
            draft_token_ids=(
                np.asarray(drafts, np.int32).reshape(len(rows), self.mtp_k)
                if self.mtp_k
                else None
            ),
            is_deferred_out=self.deferred,
        )
