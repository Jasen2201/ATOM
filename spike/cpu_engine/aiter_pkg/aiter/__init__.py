"""Test-only stand-in for aiter: the names ATOM's CPU path reads, nothing else."""

import enum
import pickle
import tempfile
import uuid

import zmq
from stand_ins import UNUSED, stand_in


class QuantType(enum.Enum):
    """aiter's quantization granularity; only the members this path reads."""

    No = 0


class MessageQueue:
    """Broadcast over one blocking ZMQ PUSH per reader: dequeue sleeps in recv."""

    kind = "queue"

    def __init__(self, n_reader, n_local_reader, max_chunk_bytes=None, **_):
        self._ctx = zmq.Context()
        tmp = tempfile.gettempdir()
        self._addrs = [
            f"ipc://{tmp}/mq-{uuid.uuid4().hex[:12]}" for _ in range(n_reader)
        ]
        self._socks = [self._ctx.socket(zmq.PUSH) for _ in self._addrs]
        for sock, addr in zip(self._socks, self._addrs):
            sock.bind(addr)

    def export_handle(self):
        return list(self._addrs)

    @classmethod
    def create_from_handle(cls, handle, rank):
        queue = cls.__new__(cls)
        queue._ctx = zmq.Context()
        queue._sock = queue._ctx.socket(zmq.PULL)
        queue._sock.connect(handle[rank])
        return queue

    def enqueue(self, obj):
        data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        for sock in self._socks:
            sock.send(data)

    def dequeue(self):
        return pickle.loads(self._sock.recv())


stand_in(
    __name__,
    {
        "": {
            "QuantType": QuantType,
            "topk_select": UNUSED,
            "mixed_sample_outer_exponential": UNUSED,
        },
        "dist.shm_broadcast": {"MessageQueue": MessageQueue},
        "ops.triton.fusions.fused_mixed_sample": {"fused_mixed_sample": UNUSED},
        "ops.triton.softmax": {"softmax": UNUSED},
        "ops.triton.topk": {"topk": UNUSED},
    },
)
