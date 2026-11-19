"""Stub ModelRunner: answers every RPC EngineCore waits on, on CPU, with no model."""

import numpy as np

from atom.model_engine.scheduler import (
    ScheduledBatch,
    ScheduledBatchOutput,
    defers_output,
)
from atom.model_engine.state_runtime import StateRuntime


def _token(position: int) -> int:
    """'a' to 'z' by position, in the byte-level fixture vocabulary."""
    return 97 + position % 26


class StubRunner:
    def __init__(self, rank, config):
        spec = config.speculative_config
        self.mtp_k = spec.num_speculative_tokens if spec is not None else 0
        self.deferred = defers_output(config)
        self.prev = None

    def get_num_blocks(self):
        return {
            "num_kvcache_blocks": 1024,
            "pool_entries": {},
            "pool_entry_bytes": {},
            "pool_entries_per_req": {},
            "state_runtime": StateRuntime().to_wire(),
        }

    def allocate_kv_cache(self, num_kvcache_blocks):
        return True

    def capture_cudagraph(self):
        return 0.0, [], 0

    def prefill_token_graphs(self):
        return False

    def freeze_gc_heap(self):
        return 0

    def release_constraints(self, request_ids):
        pass

    def release_multimodal_requests(self, request_ids):
        pass

    def exit(self):
        pass

    def dummy_execution(self):
        empty = ScheduledBatch(
            seqs={},
            num_scheduled_tokens=[],
            total_tokens_num=0,
            num_spec_step=self.mtp_k,
        )
        return self.forward(empty)

    def forward(self, batch):
        if not batch.produces_output():
            return ScheduledBatchOutput(list(batch.req_ids), [], None, None, None)
        out = self._sample(batch)
        if not self.deferred:
            return out
        # Deferred output: a step reports the previous output step's ids.
        out, self.prev = self.prev, out
        return out if out is not None else self._outputs([], [], [])

    def _sample(self, batch):
        """Every draft accepted: a verify row emits num_spec_step + 1 ids."""
        rows, drafts = [], []
        for i in range(len(batch.req_ids)):
            steps = 0 if i < batch.total_seqs_num_prefill else batch.num_spec_step
            start = int(batch.context_lens[i])
            rows.append(tuple(_token(start + j) for j in range(steps + 1)))
            drafts.append([_token(start + steps + 1 + j) for j in range(self.mtp_k)])
        return self._outputs(list(batch.req_ids), rows, drafts)

    def _outputs(self, req_ids, rows, drafts):
        n = len(rows)
        return ScheduledBatchOutput(
            req_ids,
            rows,
            num_rejected=np.zeros(n, np.int32),
            num_bonus=np.asarray([len(r) - 1 for r in rows], np.int32),
            draft_token_ids=(
                np.asarray(drafts, np.int32).reshape(n, self.mtp_k)
                if self.mtp_k
                else None
            ),
            is_deferred_out=self.deferred,
        )
