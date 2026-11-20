# SPDX-License-Identifier: MIT
"""aiter's broadcast queue over blocking ZMQ sockets: a waiting reader sleeps."""

import pickle

import zmq


class MessageQueue:
    """One PUSH socket per reader, so every reader gets every message in order."""

    def __init__(self, n_reader, n_local_reader, max_chunk_bytes=None, connect_ip=None):
        from atom.utils import get_open_zmq_ipc_path

        self._ctx = zmq.Context()
        self._handle = [get_open_zmq_ipc_path() for _ in range(n_reader)]
        self._socks = []
        for addr in self._handle:
            sock = self._ctx.socket(zmq.PUSH)
            sock.setsockopt(zmq.LINGER, 0)
            sock.bind(addr)
            self._socks.append(sock)

    def export_handle(self) -> list[str]:
        return list(self._handle)

    @classmethod
    def create_from_handle(cls, handle: list[str], rank: int) -> "MessageQueue":
        queue = cls.__new__(cls)
        queue._ctx = zmq.Context()
        queue._sock = queue._ctx.socket(zmq.PULL)
        queue._sock.connect(handle[rank])
        return queue

    def enqueue(self, obj) -> None:
        data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        for sock in self._socks:
            sock.send(data)

    def dequeue(self):
        return pickle.loads(self._sock.recv())
