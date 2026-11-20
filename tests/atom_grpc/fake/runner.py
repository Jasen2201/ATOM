# SPDX-License-Identifier: MIT
"""Fake ModelRunner: answers every RPC EngineCore waits on, on CPU, with no model.
Steps run back to back on one fake GPU at their law's price; ids follow `IdLaw`."""

import json
import math
import os
import time
from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from atom.kv_transfer.disaggregation import KVConnectorFactory, KVConnectorOutput
from atom.model_engine.scheduler import ScheduledBatchOutput, defers_output
from atom.model_engine.state_runtime import StateRuntime
from tests.atom_grpc.fake.ids import IdLaw, fingerprint
from tests.atom_grpc.fake.law import ConstantLaw, Shape, StepLaw
from tests.atom_grpc.fake.record import Recorder
from tests.atom_grpc.fake.serve import CONFIG_ENV


@dataclass(frozen=True)
class FakeConfig:
    """A fake instance's knobs: the JSON file `FAKE_ENGINE_CONFIG` names, else these."""

    acceptance_length: float = 3.51  # mean tokens per verify step
    step_s: float = 0.001  # the constant-step law: the price of every step
    law: str = ""  # a step-linear law file (JSON or .xz) to price steps by instead
    clock: str = "real"  # "virtual": time passes only while the fake GPU is waited on
    num_kvcache_blocks: int = 4096
    block_bytes: int = 0  # the KV bytes a block moves from P to D; a P instance sets it
    bandwidth_gb_s: float = 20.0  # each P rank's one link to D
    transfer_timeout_s: float = 30.0  # when a "timeout" fault fails, as Mooncake's 30 s
    faults: tuple = ()  # a D's rules: {"fault", "dp_rank"?, "prompt_prefix"?}

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


class Clock:
    """The fake GPU's time: monotonic seconds, a sleep until one, and the wall clock's
    offset from them; `monotonic` and `wall` are injectable."""

    def __init__(self, monotonic=time.monotonic, wall=time.time):
        self.monotonic, self.wall = monotonic, wall

    def now(self) -> float:
        return self.monotonic()

    def sleep_until(self, t: float) -> None:
        delay = t - self.monotonic()
        if delay > 0:
            time.sleep(delay)

    def offset(self) -> float:
        """Wall minus monotonic from the tightest of three bracketed reads: a process
        preempted between two bare reads would stamp early."""
        reads = []
        for _ in range(3):
            before, wall, after = self.monotonic(), self.wall(), self.monotonic()
            reads.append((after - before, wall - (before + after) / 2))
        return min(reads)[1]


class VirtualClock(Clock):
    """Seconds that pass only while the fake GPU is waited on, the engine's own time
    free; its stamps are those seconds, so a test reads no wall time."""

    def __init__(self):
        super().__init__(lambda: self.t, lambda: self.t)
        self.t = 0.0

    def sleep_until(self, t: float) -> None:
        self.t = max(self.t, t)


CLOCKS = {"real": Clock, "virtual": VirtualClock}


class SerialGpu:
    """One fake GPU: a step starts once it is called and the step before it ends."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.free_at = 0.0

    def run(self, price: float) -> tuple[float, float, float]:
        """Queue a step of `price` seconds: when it was called, starts and ends."""
        called = self.clock.now()
        start = max(called, self.free_at)
        self.free_at = start + price
        return called, start, self.free_at


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
        self.config = config
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
        self.id_law = IdLaw.for_model(config.model, stop)
        self.step_law = ConstantLaw(self.fake.step_s)
        if self.fake.law:
            if config.parallel_config.data_parallel_size > 1:
                raise ValueError(
                    "a step law prices DP 1 steps; a DP dummy has no price"
                )
            self.step_law = StepLaw.load(self.fake.law)
        # The ladder a decode's rung comes from, narrowed as ModelRunner narrows it.
        top = min(
            config.max_num_seqs, config.max_num_batched_tokens // (self.mtp_k + 1)
        )
        captured = [size for size in config.capture_sizes if size <= top]
        self.ladder = [] if config.enforce_eager else sorted(captured)
        self.clock = CLOCKS[self.fake.clock]()
        self.gpu = SerialGpu(self.clock)
        self.kv = np.zeros(0, np.int32)  # the token id each KV slot holds
        self.requests: dict[int, _Request] = {}
        self.pending = None  # deferred output: the last output step's (output, end)
        self.dp_rank = config.parallel_config.data_parallel_rank
        self.record = Recorder.from_env() if rank == 0 else None  # the engine's output
        self.structured = None  # the reply to `prepare_structured_output`
        self.connector = None  # the worker half of the instance's KV connector
        if config.kv_transfer_config:
            self.connector = KVConnectorFactory.create_connector(config, "worker")

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
        if self.connector is not None:
            self.connector.register_kv_caches({"token_ids": self.kv})
        return True

    def process_kvconnector_output(self, metadata) -> None:
        self.connector.start_load_kv(metadata)

    def async_proc_aggregation(self) -> KVConnectorOutput:
        return self.connector.get_finished()

    def capture_cudagraph(self):
        return 0.0, [], 0

    def prefill_token_graphs(self) -> bool:
        return False

    def freeze_gc_heap(self) -> int:
        return 0

    def prepare_structured_output(
        self,
        schema,
        tool_format,
        reasoning_end,
        fingerprint,
        vocab_size,
        request_ids=(),
    ) -> dict:
        """A runner's reply, from the fixture's tokenizer and vocabulary. No grammar is
        compiled: no fake step follows one."""
        if self.structured is None:
            from atom.model_engine.llm_engine import load_tokenizer
            from atom.structured_output import (
                structured_stop_token_ids,
                tokenizer_fingerprint,
            )

            config, hf = self.config, self.config.hf_config
            tokenizer = load_tokenizer(config.model, config.trust_remote_code)
            stops = structured_stop_token_ids(config, tokenizer)
            self.structured = {
                "ready": True,
                "fingerprint": tokenizer_fingerprint(tokenizer, stops),
                "vocab_size": (getattr(hf, "text_config", None) or hf).vocab_size,
            }
        return self.structured

    def release_constraints(self, request_ids) -> None:
        pass

    def release_multimodal_requests(self, request_ids) -> None:
        pass

    def exit(self) -> None:
        pass

    def dummy_execution(self) -> ScheduledBatchOutput:
        _, _, end = self.gpu.run(self.fake.step_s)  # DP only, where the law is constant
        return self._hand_back(self._output([], [], [], []), end)

    def forward(self, batch) -> ScheduledBatchOutput:
        shape = self._shape(batch)
        price = self.step_law.price(shape)
        called, start, end = self.gpu.run(price)
        output, emitted = self._step(batch)
        if self.record is not None:
            offset = self.clock.offset()  # one offset for the step's three times
            self.record.step(
                self.dp_rank,
                emitted,
                end + offset,
                called=called + offset,
                start=start + offset,
                price=price,
                requests=[[r, *row] for r, row in zip(batch.req_ids, shape.requests)],
                rung=shape.rung,
                samples=shape.samples,
            )
        if not shape.samples:
            return ScheduledBatchOutput(list(batch.req_ids), [], None, None, None)
        return self._hand_back(output, end)

    def _shape(self, batch) -> Shape:
        """What a law prices `batch` by; a decode's rung is the narrowest captured size
        holding its requests, as `ForwardMode.decide` picks one at DP 1."""
        prefill = batch.total_seqs_num_prefill
        rows = zip(batch.num_scheduled_tokens.tolist(), batch.context_lens.tolist())
        requests = tuple((q, c, i >= prefill) for i, (q, c) in enumerate(rows))
        rung = None
        if not prefill:
            rung = next((size for size in self.ladder if size >= len(requests)), None)
        return Shape(requests, rung, batch.produces_output())

    def _hand_back(self, output, end: float) -> ScheduledBatchOutput:
        """The output a step returns once it is ready: deferred, the step before's."""
        if self.deferred:
            empty = (self._output([], [], [], []), 0.0)
            self.pending, (output, end) = (output, end), self.pending or empty
        self.clock.sleep_until(end)
        return output

    def _step(self, batch) -> tuple[ScheduledBatchOutput, list]:
        """Write the step's KV, then emit each row's ids at their true positions;
        returns the output and each row's (req_id, ids) but a middle chunk's filler."""
        k = batch.num_spec_step
        token_ends = np.cumsum(batch.num_scheduled_tokens)
        rows, drafts, rejected, emitted = [], [], [], []
        for i, req_id in enumerate(batch.req_ids):
            table, end = batch.block_tables[i], int(batch.context_lens[i])
            if i < batch.total_seqs_num_prefill:
                start, stop = int(batch.num_cached_tokens[i]), int(token_ends[i])
                self._write(
                    table, start, batch.scheduled_tokens[stop - end + start : stop]
                )
                if batch.is_final_chunk is not None and not batch.is_final_chunk[i]:
                    # A middle chunk samples too, and the scheduler drops it.
                    rows.append((self.id_law.fill[0],))
                    drafts.append([self.id_law.fill[0]] * self.mtp_k)
                    rejected.append(0)
                    continue
                req, first, count, reject = self._admit(req_id, table, end), end, 1, 0
            else:
                req = self._decoding(req_id, table, end - 1 - k)
                first, count, reject = end, 1, 0
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
            emitted.append((req_id, ids[:count]))
        return self._output(list(batch.req_ids), rows, drafts, rejected), emitted

    def _admit(self, req_id: int, table, end: int) -> _Request:
        """A final prefill chunk: a new request's prompt is what its KV slots hold."""
        req = self.requests.get(req_id)
        if req is None:
            prompt = self.kv[self._slots(table, 0, end)].tolist()
            req = self.requests[req_id] = _Request(fingerprint(prompt), end)
        req.rejected = 0
        return req

    def _decoding(self, req_id: int, table, prompt_len: int) -> _Request:
        """A decoding request; on D, one first seen decoding has its prompt in the KV
        it received, ahead of P's first token and the drafts D verifies."""
        req = self.requests.get(req_id)
        if req is None:
            if self.connector is None or self.connector.is_producer:
                raise RuntimeError(
                    f"request {req_id} decodes with no prefill seen here"
                )
            prompt = self.kv[self._slots(table, 0, prompt_len)].tolist()
            req = self.requests[req_id] = _Request(fingerprint(prompt), prompt_len)
        return req

    def _ids(self, req: _Request, start: int, stop: int) -> list[int]:
        return self.id_law.ids(req.fingerprint, start - req.prompt_len, stop - start)

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
