# SPDX-License-Identifier: MIT
"""Fake Mooncake: Mooncake's own scheduler half over a transport that copies a request's
fake KV, a token id per slot, from its P rank to its D rank over a timed link."""

import os
import threading
import time
from dataclasses import dataclass

import msgpack
import numpy as np
import zmq

from atom.kv_transfer.disaggregation.base import KVConnectorBase
from atom.kv_transfer.disaggregation.chunked_prefill import PrefillHandoff
from atom.kv_transfer.disaggregation.mooncake.endpoint import DEFAULT_HANDSHAKE_PORT
from atom.kv_transfer.disaggregation.mooncake.mooncake_connector import (
    PREFILL_LOOKUP_TIMEOUT,
    MooncakeConnectorScheduler,
    _receive_block_offset,
)
from atom.kv_transfer.disaggregation.port_offset import side_channel_port_offset
from atom.kv_transfer.disaggregation.types import ConnectorCompletion, KVConnectorOutput
from atom.utils import envs
from atom.utils.network import get_ip
from tests.atom_grpc.fake.record import Recorder
from tests.atom_grpc.fake.runner import Clock, FakeConfig
from tests.atom_grpc.fake.serve import LINKS_ENV

FAULT = "fake_fault"  # in D's kv_transfer_params: "timeout", or any other value to fail
FAULTS = ("fail", "timeout")
RULE_KEYS = {"fault", "dp_rank", "prompt_prefix"}
COMPLETIONS = {"admit": "pd_admission_ready", "safe": "pd_source_safe"}


class FakeMooncakeScheduler(MooncakeConnectorScheduler):
    """Mooncake's scheduler half; a D rank also names each receive's fault, its
    `FAULT` tag or the first fake config rule that its DP rank and prompt match."""

    def __init__(self, config):
        super().__init__(config)
        self.rules = FakeConfig.from_env().faults
        for rule in self.rules:
            if rule.keys() - RULE_KEYS or rule.get("fault") not in FAULTS:
                raise ValueError(
                    f"fault rule {rule}: keys {RULE_KEYS}, one of {FAULTS}"
                )
        self.faults = {}  # receives allocated since the last metadata, by request id

    def update_state_after_alloc(self, seq) -> None:
        params = seq.kv_transfer_params or {}
        if params.get("do_remote_prefill"):  # a receive; Mooncake clears the flag
            rules = (r["fault"] for r in self.rules if self._matches(r, seq))
            fault = params.get(FAULT) or next(rules, None)
            if fault:
                self.faults[seq.id] = fault
        super().update_state_after_alloc(seq)

    def _matches(self, rule: dict, seq) -> bool:
        prefix = list(rule.get("prompt_prefix", []))
        return rule.get("dp_rank", self.dp_rank) == self.dp_rank and (
            list(seq.prompt_token_ids[: len(prefix)]) == prefix
        )

    def build_connector_meta(self):
        meta = super().build_connector_meta()
        meta.faults = {
            r: self.faults.pop(r) for r in meta.reqs_to_recv if r in self.faults
        }
        return meta


@dataclass
class _Source:
    """A P request's KV as D asks for it: under chunked transfer, ready at P's handoff."""

    req_id: int
    block_ids: list[int]
    chunked: bool
    handoff: dict | None = None
    cancelled: bool = False


class FakeMooncakeWorker(KVConnectorBase):
    """D asks its P rank over an ipc socket named by Mooncake's port; P answers once the
    KV is there, the copy timed on P's one link, a fault failing it instead."""

    def __init__(self, config):
        kv_config = config.kv_transfer_config
        parallel = config.parallel_config
        sizes = (config.tensor_parallel_size, config.pipeline_parallel_size)
        if sizes + (config.decode_context_parallel_size,) != (1, 1, 1):
            raise ValueError("the fake Mooncake transport moves TP 1, PP 1, DCP 1 KV")
        self.is_producer = kv_config.get("kv_role", "kv_producer") == "kv_producer"
        self.fake = FakeConfig.from_env()
        self.block_size = config.kv_cache_block_size
        self.links = os.environ[LINKS_ENV]
        self.recorder = Recorder.from_env()
        self.kv = None  # the runner's token id per KV slot
        if not self.is_producer:
            self.peers = {}  # P rank address -> DEALER socket
            self.receives = {}  # request id -> (transfer id, destination blocks)
            self.replies = []
            return
        if self.fake.block_bytes <= 0:
            raise ValueError("a P instance's fake config sets block_bytes")
        self.lock = threading.Lock()
        self.sources = {}  # by transfer id, a relay's being its P request id
        self.admissions = {}  # transfer id -> chunked P request waiting for its D
        self.admitted = set()
        self.due = []  # (monotonic time, kind, id) completions, reported from then
        self.link_free = 0.0
        port = kv_config.get("handshake_port", DEFAULT_HANDSHAKE_PORT)
        port += side_channel_port_offset(
            parallel.data_parallel_rank, 0, 1, 0, 1, parallel.data_parallel_size
        )
        sock = zmq.Context.instance().socket(zmq.ROUTER)
        sock.bind(self._address(get_ip(), port))
        threading.Thread(target=self._listen, args=(sock,), daemon=True).start()

    def _address(self, host: str, port: int) -> str:
        return f"ipc://{self.links}/mooncake-{host}-{port}"

    def _slots(self, blocks) -> np.ndarray:
        blocks = np.asarray(blocks, np.int64)[:, None] * self.block_size
        return (blocks + np.arange(self.block_size)).ravel()

    def register_kv_caches(self, kv_caches, transfer_tensors=None, num_blocks=None):
        self.kv = kv_caches["token_ids"]

    def start_load_kv(self, metadata) -> None:
        if self.is_producer:
            with self.lock:
                self._save(metadata)
            return
        for req_id, meta in metadata.reqs_to_recv.items():
            num_src = len(meta.remote_block_ids or [])
            offset = _receive_block_offset(
                len(meta.local_block_ids),
                num_src,
                meta.num_computed_blocks,
                1,
                meta.chunked_transfer,
            )
            dst = meta.local_block_ids[offset:]
            port = meta.remote_handshake_port + side_channel_port_offset(
                meta.remote_dp_rank,
                0,
                meta.tp_size,
                0,
                meta.remote_pp_size,
                meta.remote_dp_size,
            )
            address = self._address(meta.remote_host, port)
            if address not in self.peers:
                self.peers[address] = zmq.Context.instance().socket(zmq.DEALER)
                self.peers[address].connect(address)
            request = {
                "transfer_id": meta.transfer_id,
                "request_id": req_id,
                "num_computed_blocks": offset,
                "num_blocks": len(dst),
                "fault": metadata.faults.get(req_id),
            }
            self.peers[address].send(msgpack.dumps(request))
            self.receives[req_id] = (meta.transfer_id, dst)

    def get_finished(self) -> KVConnectorOutput:
        out, now = KVConnectorOutput(), time.monotonic()
        if self.is_producer:
            with self.lock:
                due = [d for d in self.due if d[0] <= now]
                self.due = [d for d in self.due if d[0] > now]
            for _, kind, key in due:
                if kind == "sent":
                    out.finished_sending.add(key)
                else:
                    completion = ConnectorCompletion(COMPLETIONS[kind], key, True)
                    out.connector_completions.add(completion)
            return out
        for sock in self.peers.values():
            while sock.poll(0):
                self.replies.append(msgpack.loads(sock.recv()))
        hold = envs.ATOM_PD_FAILED_WRITE_HOLD_S  # as Mooncake holds a failed receive
        for reply in list(self.replies):
            end = reply["start"] + reply["seconds"]
            if end + (0.0 if reply["ok"] else hold) > now:
                continue
            self.replies.remove(reply)
            req_id = reply["request_id"]
            transfer_id, dst = self.receives.pop(req_id)
            if reply["ok"]:
                self.kv[self._slots(dst)] = np.frombuffer(reply["tokens"], np.int32)
                out.finished_recving.add(req_id)
                if reply["handoff"] is not None:
                    handoff = PrefillHandoff.from_wire(req_id, reply["handoff"])
                    out.received_handoffs[req_id] = handoff
            else:
                out.failed_recving.add(req_id)
            if self.recorder is not None:
                wall = Clock().offset()
                self.recorder.receive(
                    req_id,
                    now + wall,
                    transfer_id=transfer_id,
                    blocks=len(dst),
                    bytes=reply["bytes"],
                    seconds=reply["seconds"],
                    start=reply["start"] + wall,
                    end=end + wall,
                    ok=reply["ok"],
                )
        return out

    def _save(self, metadata) -> None:
        """P: register the sources and chunked admissions the scheduler handed over."""
        for req_id, transfer_id in metadata.chunked_admissions.items():
            self.admissions[transfer_id] = req_id
        for req_id, meta in metadata.reqs_to_save.items():
            key = meta.transfer_id if meta.chunked_transfer else req_id
            source = self.sources.setdefault(
                key, _Source(req_id, meta.local_block_ids, meta.chunked_transfer)
            )
            source.handoff = meta.prefill_handoff or source.handoff
            if meta.prefill_aborted:
                source.cancelled = True
                self.admissions.pop(key, None)
                self.due.append((0.0, "safe", req_id))

    def _listen(self, sock) -> None:
        """P: queue D's requests, and answer each once it can be served."""
        waiting = []
        while True:
            if sock.poll(10):
                identity, body = sock.recv_multipart()
                waiting.append((identity, msgpack.loads(body), time.monotonic()))
            with self.lock:
                for item in list(waiting):
                    reply = self._serve(*item[1:])
                    if reply is not None:
                        waiting.remove(item)
                        sock.send_multipart([item[0], msgpack.dumps(reply)])

    def _serve(self, request: dict, arrived: float) -> dict | None:
        """P: D's reply, None until its source is ready. A fault, a cancelled source, a
        block count unlike D's, or an orphan past Mooncake's lookup timeout fails it."""
        transfer_id, now = request["transfer_id"], time.monotonic()
        if transfer_id in self.admissions:  # this D lets its chunked P allocate
            self.admitted.add(transfer_id)
            self.due.append((now, "admit", self.admissions.pop(transfer_id)))
        source = self.sources.get(transfer_id)
        if source is None:
            if transfer_id in self.admitted or now - arrived < PREFILL_LOOKUP_TIMEOUT:
                return None
        elif source.chunked and not (source.handoff or source.cancelled):
            return None
        reply = {"request_id": request["request_id"], "ok": False, "bytes": 0}
        reply |= {"start": now, "seconds": 0.0}
        if source is None or source.cancelled:
            self.sources.pop(transfer_id, None)
            return reply
        del self.sources[transfer_id]
        blocks = source.block_ids[request["num_computed_blocks"] :]
        reply["bytes"] = len(blocks) * self.fake.block_bytes
        if request["fault"] == "timeout":
            reply["seconds"] = self.fake.transfer_timeout_s
        elif not request["fault"] and len(blocks) == request["num_blocks"]:
            reply["start"] = max(now, self.link_free)
            reply["seconds"] = reply["bytes"] / self.fake.bandwidth_gb_s / 1e9
            self.link_free = reply["start"] + reply["seconds"]
            reply |= {"ok": True, "handoff": source.handoff}
            reply["tokens"] = self.kv[self._slots(blocks)].tobytes()
        kind, key = ("safe", source.req_id) if source.chunked else ("sent", transfer_id)
        self.due.append((reply["start"] + reply["seconds"], kind, key))
        return reply
