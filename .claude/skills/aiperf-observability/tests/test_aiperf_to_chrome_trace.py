import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "aiperf_to_chrome_trace.py"
)
_spec = importlib.util.spec_from_file_location("aiperf_to_chrome_trace", _MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

build_trace = _module.build_trace
load_records = _module.load_records


def metric(value, unit="ms"):
    return {"value": value, "unit": unit}


def record(
    *,
    request_id,
    start_ns,
    end_ns,
    root_id="root-a",
    phase="profiling",
    ttft_ms=10.0,
    decode_ms=90.0,
):
    return {
        "metadata": {
            "session_num": request_id,
            "x_request_id": f"request-{request_id}",
            "x_correlation_id": f"correlation-{root_id}",
            "root_correlation_id": root_id,
            "conversation_id": f"conversation-{root_id}",
            "turn_index": request_id,
            "request_start_ns": start_ns,
            "request_end_ns": end_ns,
            "benchmark_phase": phase,
        },
        "metrics": {
            "time_to_first_token": metric(ttft_ms),
            "full_decode_duration": metric(decode_ms),
            "input_sequence_length": metric(100, "tokens"),
            "output_sequence_length": metric(20, "tokens"),
        },
    }


def test_build_trace_filters_warmup_and_emits_request_phases_and_gap():
    records = [
        record(
            request_id=0,
            start_ns=1_000_000_000,
            end_ns=1_100_000_000,
            phase="warmup",
        ),
        record(
            request_id=1,
            start_ns=2_000_000_000,
            end_ns=2_100_000_000,
            ttft_ms=25.0,
        ),
        record(
            request_id=2,
            start_ns=2_300_000_000,
            end_ns=2_500_000_000,
            ttft_ms=50.0,
            decode_ms=150.0,
        ),
    ]

    trace = build_trace(records, session_id="root-a")
    events = trace["traceEvents"]
    names = [event["name"] for event in events]

    assert any(name.startswith("request #1") for name in names)
    assert any(name.startswith("request #2") for name in names)
    assert not any(name.startswith("request #0") for name in names)
    assert "session idle" in names

    request_1 = next(
        event for event in events if event["name"].startswith("request #1")
    )
    assert request_1["ts"] == 0
    assert request_1["dur"] == 100_000
    assert request_1["args"]["time_to_first_token_ms"] == 25.0
    assert request_1["args"]["input_tokens"] == 100
    assert request_1["args"]["usage_prompt_tokens"] is None
    assert request_1["args"]["output_tokens"] == 20
    assert request_1["args"]["usage_completion_tokens"] is None
    assert "input=100 tok" in request_1["name"]
    assert "output=20 tok" in request_1["name"]

    prefill = next(event for event in events if event["name"] == "prefill #1")
    decode = next(event for event in events if event["name"] == "decode #1")
    assert prefill["dur"] == 25_000
    assert decode["dur"] == 75_000


def test_load_records_reads_jsonl(tmp_path):
    path = tmp_path / "profile_export.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(record(request_id=1, start_ns=10, end_ns=20)),
                "",
            ]
        )
    )

    assert len(load_records(path)) == 1


def test_build_trace_can_reuse_fixed_concurrency_slots():
    records = [
        record(
            request_id=1,
            start_ns=1_000_000_000,
            end_ns=1_100_000_000,
            root_id="tree-a",
        ),
        record(
            request_id=2,
            start_ns=1_200_000_000,
            end_ns=1_300_000_000,
            root_id="tree-b",
        ),
    ]

    trace = build_trace(records, concurrency=1)
    request_events = [
        event for event in trace["traceEvents"] if event["name"].startswith("request #")
    ]

    assert trace["metadata"]["slot_count"] == 1
    assert {event["args"]["slot_index"] for event in request_events} == {0}
    assert trace["metadata"]["tree_count"] == 2


def test_build_trace_can_show_wait_prefill_overlap_on_inspected_slot():
    item = record(
        request_id=1,
        start_ns=1_000_000_000,
        end_ns=1_100_000_000,
        root_id="tree-a",
        ttft_ms=25.0,
    )
    item["metrics"]["http_req_sending"] = metric(5.0)
    item["metrics"]["http_req_waiting"] = metric(30.0)

    trace = build_trace([item], concurrency=1, inspect_slot=0)
    events = trace["traceEvents"]
    overlap = next(event for event in events if "wait∩prefill" in event["name"])

    assert any(
        "wait/TTFB" in event["args"]["name"]
        for event in events
        if event["name"] == "thread_name"
    )
    assert overlap["args"]["overlap_ms"] == 20.0
    assert overlap["args"]["prefill_covered_pct"] == 80.0


def test_build_trace_can_analyze_all_slots_and_link_ttft_to_slot_rows():
    records = []
    for request_id, root_id in enumerate(("tree-a", "tree-b"), start=1):
        item = record(
            request_id=request_id,
            start_ns=request_id * 1_000_000_000,
            end_ns=request_id * 1_000_000_000 + 100_000_000,
            root_id=root_id,
            ttft_ms=25.0,
        )
        item["metrics"]["http_req_sending"] = metric(5.0)
        item["metrics"]["http_req_waiting"] = metric(30.0)
        item["metrics"]["usage_prompt_cache_read_tokens"] = metric(
            80 if root_id == "tree-a" else 70, "tokens"
        )
        records.append(item)

    trace = build_trace(records, concurrency=2, inspect_all_slots=True)
    events = trace["traceEvents"]
    track_names = {
        event["args"]["name"] for event in events if event["name"] == "thread_name"
    }

    assert "all slots • TTFT" in track_names
    assert "all slots • decode" in track_names
    assert "all slots • prefill+decode" in track_names
    assert "slot 00 wait∩prefill" in track_names
    assert "slot 01 wait∩prefill" in track_names
    assert not any("wait/TTFB" in name for name in track_names)
    assert len([event for event in events if " TTFT #" in event["name"]]) == 2
    assert len([event for event in events if event.get("ph") in {"s", "f"}]) == 8
    ttft = next(event for event in events if " TTFT #" in event["name"])
    assert ttft["args"]["prompt_cache_read_tokens"] == 80
    assert ttft["args"]["cache_read_ratio_pct"] == 80.0
    anomalous_ttft = next(
        event
        for event in events
        if " TTFT #" in event["name"] and event["args"]["cache_read_ratio_pct"] == 70.0
    )
    assert anomalous_ttft["args"]["cache_anomaly"] is True
    assert anomalous_ttft["cname"] == "terrible"
    assert (
        len(
            [
                event
                for event in events
                if event.get("tid") == 9_001 and " decode #" in event["name"]
            ]
        )
        == 2
    )
    combined = [
        event for event in events if event.get("tid") == 9_002 and event["ph"] == "X"
    ]
    assert len(combined) == 4
    assert {event["cname"] for event in combined} == {
        "yellow",
        "thread_state_runnable",
        "terrible",
    }
