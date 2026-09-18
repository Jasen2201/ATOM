# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

"""P/D source ownership, using the actual scheduler connectors without RDMA."""

import importlib
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiter_stub import stubbed_aiter

from atom.kv_transfer.disaggregation.types import KVConnectorOutput
from atom.model_engine.scheduler import ScheduledBatchOutput
from atom.model_engine.sequence import SequenceStatus


@pytest.fixture
def connector(monkeypatch):
    with stubbed_aiter():
        module = importlib.import_module(
            "atom.kv_transfer.disaggregation.mooncake.mooncake_connector"
        )
    monkeypatch.setattr(module, "get_open_port", lambda: 1234)
    monkeypatch.setattr(module, "get_ip", lambda: "127.0.0.1")
    return module.MooncakeConnectorScheduler(
        SimpleNamespace(
            kv_transfer_config={"kv_role": "kv_producer"},
            tensor_parallel_size=1,
            pipeline_parallel_size=1,
            decode_context_parallel_size=1,
            kv_cache_block_size=4,
            parallel_config=SimpleNamespace(data_parallel_size=1, data_parallel_rank=0),
        )
    )


@pytest.fixture
def seq():
    return SimpleNamespace(
        id=7,
        kv_transfer_params={"do_remote_decode": True},
        block_table=[1, 2],
        state_slots=[],
        output_tokens=[10],
        leave_reason="length",
        kv_transfer_params_output=None,
    )


@pytest.mark.parametrize("early", [False, True])
def test_published_sources_wait_for_send_without_rearming_early_completion(
    connector, seq, early
):
    connector.update_state_after_alloc(seq)
    connector.build_connector_meta()
    assert not connector.should_defer_free(seq)  # Unadvertised plan is cancellable.
    if early:
        connector.process_completions(KVConnectorOutput(finished_sending={str(seq.id)}))
    connector.request_finished(seq)
    assert seq.kv_transfer_params_output["remote_block_ids"] == [1, 2]
    assert connector.should_defer_free(seq) is not early
    if not early:
        connector.process_completions(KVConnectorOutput(finished_sending={seq.id}))
    assert not connector.should_defer_free(seq)
    assert not connector._send_sources


@pytest.mark.parametrize("params", [None, {}, {"do_remote_decode": False}])
def test_producer_role_without_a_send_does_not_hold_or_publish(connector, seq, params):
    seq.kv_transfer_params = params
    connector.update_state_after_alloc(seq)
    connector.request_finished(seq)
    assert not connector.should_defer_free(seq)
    assert seq.kv_transfer_params_output is None
    assert not connector._send_sources


def test_aborted_unpublished_send_is_cancelled(connector, seq):
    connector.update_state_after_alloc(seq)
    seq.leave_reason = "aborted"
    connector.request_finished(seq)
    assert not connector.should_defer_free(seq)
    assert seq.kv_transfer_params_output is None
    assert not connector._send_sources
    connector.source_blocks_released(seq)
    assert not connector.build_connector_meta().reqs_to_save


def test_preemption_discards_the_plan_and_old_early_completion(connector, seq):
    connector.update_state_after_alloc(seq)
    connector.process_completions(KVConnectorOutput(finished_sending={seq.id}))
    assert not connector.should_defer_free(seq)
    connector.source_blocks_released(seq)
    assert not connector.build_connector_meta().reqs_to_save
    # A report without a live plan cannot complete a future allocation.
    result = connector.process_completions(KVConnectorOutput(finished_sending={seq.id}))
    assert not result.finished_sending
    seq.block_table = [3, 4]
    connector.update_state_after_alloc(seq)
    connector.request_finished(seq)
    assert connector.should_defer_free(seq)
    assert seq.kv_transfer_params_output["remote_block_ids"] == [3, 4]


def test_consumer_finishes_without_creating_a_send(connector, seq):
    connector.is_producer = False
    seq.kv_transfer_params = {"transfer_id": 88}
    connector.update_state_after_alloc(seq)
    connector.request_finished(seq)
    assert not connector.request_id_to_transfer_id
    assert not connector.transfer_id_to_request_id
    assert not connector.should_defer_free(seq)
    assert seq.kv_transfer_params_output["remote_block_ids"] == [1, 2]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("stop", ["none", "eos", "length", "stop_sequence"])
def test_aborted_producer_never_publishes_even_when_forward_also_stops(
    connector, scheduler, seq_factory, streaming, stop
):
    seq = seq_factory(
        [3, 4, 5, 6],
        stop_token_sequences=[[10]] if stop == "stop_sequence" else [],
    )
    scheduler.add(seq)
    scheduler.schedule()
    seq.kv_transfer_params = {"do_remote_decode": True}
    connector.update_state_after_alloc(seq)
    scheduler.kv_connector = connector
    connector.request_finished = Mock(wraps=connector.request_finished)
    seq.status = SequenceStatus.ABORTED
    seq.max_tokens = 1 if stop == "length" else 100
    stream = Queue() if streaming else None

    finished = scheduler.postprocess(
        [seq],
        ScheduledBatchOutput(
            req_ids=[seq.id],
            token_ids=[[2 if stop == "eos" else 10]],
            num_rejected=None,
            num_bonus=None,
            draft_token_ids=None,
        ),
        stream_output_queue=stream,
    )

    assert finished == [seq]
    assert seq.leave_reason == "aborted"
    connector.request_finished.assert_called_once_with(seq)
    assert seq.kv_transfer_params_output is None
    assert not connector._send_sources
    assert not seq.block_table
    assert scheduler.is_finished()
    if streaming:
        output = stream.get_nowait()[0][1]
        assert output.finished and output.finish_reason == "aborted"
        assert output.kv_transfer_params_output is None
