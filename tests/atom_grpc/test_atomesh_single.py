# SPDX-License-Identifier: MIT
"""atomesh in gRPC mode over one fake instance: what reaches ATOM and the client, what a
disconnect and a refusal do, and the one command that starts and stops it all."""

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import grpc
import psutil
import pytest
from transformers import AutoTokenizer

from atom.entrypoints.grpc import atom_grpc_pb2 as pb
from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from tests.atom_grpc.fake.record import RECORD, EngineRecord
from tests.atom_grpc.harness.atomesh import ATOMESH_BIN, Atomesh, RouterSpec
from tests.atom_grpc.harness.launch import load
from tests.atom_grpc.harness.topologies import TOPOLOGIES, Topology, group_members

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
ROUTES = {"chat": "/v1/chat/completions", "completions": "/v1/completions"}
# atomesh's own tokenizer, template and runtime flags, which no router here is given
FLAGS = (
    "--model-path",
    "--tokenizer-path",
    "--chat-template",
    "--backend",
    "--runtime",
)
SYLLABLES = [c + v for c in "bcdfghjklmnprstvwxyz" for v in "aeiou"]  # fixture words
MAX_TOKENS = 50000  # far past where a disconnected request stops
WAIT_S = 120
# A template of its own: the fixture's own puts no <|end_of_msg|> after a message.
OWN_TEMPLATE = (
    "{{ bos_token }}{% for m in messages %}<|user|>{{ m['content'] }}<|end_of_msg|>"
    "{% endfor %}{% if add_generation_prompt %}<|assistant|>{% endif %}"
)


@pytest.fixture(scope="module")
def tokenizer():
    return AutoTokenizer.from_pretrained(FIXTURE)


def words(count: int, lead: int = 0) -> str:
    """`count` fixture words; prompts with different leads share no first word."""
    return " ".join(SYLLABLES[(lead + i) % len(SYLLABLES)] for i in range(count))


def body(route: str, model: str, content: str, **fields) -> dict:
    """A request to `route`: `content` its prompt, or a chat's one user message."""
    if route == "completions":
        return {"model": model, "prompt": content, **fields}
    chat = {"model": model, "messages": [{"role": "user", "content": content}]}
    if fields.get("stream"):
        chat["stream_options"] = {"include_usage": True}
    return chat | fields


def model_info(instance) -> pb.GetModelInfoResponse:
    with grpc.insecure_channel(instance.grpc) as channel:
        stub = pb_grpc.AtomEngineStub(channel)
        return stub.GetModelInfo(pb.GetModelInfoRequest(), timeout=60)


def rendered_ids(router, model, route, content, tokenizer, template) -> list[int]:
    """The ids atomesh renders for `content`: its tokenizer's, on the prompt or on the
    chat as `template` renders it."""
    text = content
    if route == "chat":
        chat = [{"role": "user", "content": content}]
        text = tokenizer.apply_chat_template(
            chat, chat_template=template, tokenize=False, add_generation_prompt=True
        )
    status, reply = router.post("/v1/tokenize", {"model": model, "prompt": text})
    assert status == 200, reply
    return reply["tokens"]


def send(router, route: str, request: dict) -> tuple[int, list[dict]]:
    """POST `request`: its status and replies, a stream's data events in order."""
    call = urllib.request.Request(
        router.url + ROUTES[route],
        data=json.dumps(request).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(call, timeout=600) as reply:
            status, text = reply.status, reply.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, [error.read().decode(errors="replace")]
    if not request.get("stream"):
        return status, [json.loads(text)]
    events = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
    return status, [json.loads(event) for event in events if event != "[DONE]"]


def answer(route: str, replies: list[dict]) -> tuple[list[str], str | None, dict]:
    """The text pieces the replies carry in order, the finish_reason and the usage."""
    pieces, finish, usage = [], None, None
    for reply in replies:
        usage = reply.get("usage") or usage
        for choice in reply["choices"]:
            message = choice.get("message") or choice.get("delta") or {}
            piece = message.get("content") if route == "chat" else choice["text"]
            pieces += [piece] if piece else []
            finish = choice["finish_reason"] or finish
    return pieces, finish, usage


def metrics(instance) -> dict[str, float]:
    """The instance's unlabelled `atom:` gauges and counters from /metrics."""
    with urllib.request.urlopen(instance.url + "/metrics", timeout=60) as reply:
        lines = reply.read().decode().splitlines()
    found = (re.fullmatch(r"(atom:\w+) (\S+)", line) for line in lines)
    return {match[1]: float(match[2]) for match in found if match}


def loads(router) -> list[int]:
    """atomesh's in-flight count of each worker."""
    status, listing = router.get("/workers")
    assert status == 200, listing
    return [worker["load"] for worker in listing["workers"]]


def wait_for(condition, what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"no {what} within {WAIT_S} s")
        time.sleep(0.2)


def idle(instance, router) -> bool:
    found = metrics(instance)
    running = found["atom:kv_cache_blocks_used"] + found["atom:requests_running"]
    return running == 0 and not any(loads(router))


@pytest.mark.topology("single")
def test_atomesh_finds_atom_over_grpc_and_takes_its_tokenizer_and_template(topology):
    router, instance = topology.routers["mesh"], topology.instances["single"]
    argv = psutil.Process(router.pid).cmdline()
    assert [arg for arg in argv if arg.partition("=")[0] in FLAGS] == []
    info = model_info(instance)
    status, listing = router.get("/workers")
    assert status == 200, listing
    (worker,) = listing["workers"]
    assert worker["url"] == f"grpc://{instance.grpc}"
    found = (worker["connection_mode"], worker["runtime_type"], worker["is_healthy"])
    assert found == ("gRPC", "atom", True)
    labels = worker["metadata"]
    assert labels["tokenizer_path"] == info.tokenizer_path
    assert labels["chat_template"] == info.chat_template
    status, loaded = router.get("/v1/tokenizers")
    assert status == 200, loaded
    (entry,) = loaded["tokenizers"]
    assert worker["model_id"] == entry["name"] == info.served_model_name
    assert entry["source"] == info.tokenizer_path


@pytest.mark.topology("single")
@pytest.mark.parametrize("stream", [False, True], ids=["unary", "stream"])
@pytest.mark.parametrize("route", ROUTES)
def test_atom_gets_what_atomesh_renders_and_the_client_every_token_once_in_order(
    topology, tokenizer, route, stream
):
    router, instance = topology.routers["mesh"], topology.instances["single"]
    info, content = model_info(instance), words(16, lead=2 * [*ROUTES].index(route))
    content += " Hello" if stream else " world"  # one prompt per case
    request = body(route, info.served_model_name, content, max_tokens=24, stream=stream)
    status, replies = send(router, route, request)
    assert status == 200, replies

    pieces, finish, usage = answer(route, replies)
    model = info.served_model_name
    ids = rendered_ids(router, model, route, content, tokenizer, info.chat_template)
    (admitted,) = EngineRecord(instance.workdir / RECORD).with_prompt(ids)
    assert (finish, usage["prompt_tokens"], usage["completion_tokens"]) == (
        "length",
        len(ids),
        24,
    )
    # Each id is one word, so a lost, repeated or reordered id or a lost space shows.
    assert "".join(pieces) == tokenizer.decode(admitted.output_ids[:24])
    assert len(pieces) > 1 if stream else len(pieces) == 1


def open_request(router, route: str, request: dict) -> http.client.HTTPConnection:
    """`request` sent; a stream's first event read, so its response has begun."""
    address = urllib.parse.urlsplit(router.url)
    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=60)
    connection.request(
        "POST", ROUTES[route], json.dumps(request), {"Content-Type": "application/json"}
    )
    if request["stream"]:
        reply = connection.getresponse()
        assert reply.status == 200
        assert reply.readline().startswith(b"data: ")
    return connection


@pytest.mark.topology("single")
@pytest.mark.parametrize("stream", [False, True], ids=["unary", "stream"])
@pytest.mark.parametrize("route", ROUTES)
def test_a_client_disconnect_aborts_the_request_at_atom_and_atomesh_load_returns_to_0(
    topology, tokenizer, route, stream
):
    router, instance = topology.routers["mesh"], topology.instances["single"]
    info, content = model_info(instance), words(16, lead=4 + [*ROUTES].index(route))
    content += " Hello" if stream else " world"  # one prompt per case
    wait_for(lambda: idle(instance, router), "idle instance and router")
    request = body(route, info.served_model_name, content, max_tokens=MAX_TOKENS)
    connection = open_request(router, route, request | {"stream": stream})

    def running() -> bool:
        blocks = metrics(instance)["atom:kv_cache_blocks_used"]
        return blocks > 0 and loads(router) == [1]

    wait_for(running, "the request holding blocks and its worker's load")
    connection.sock.shutdown(socket.SHUT_RDWR)
    connection.close()
    wait_for(lambda: idle(instance, router), "abort freeing every block and load")
    model = info.served_model_name
    ids = rendered_ids(router, model, route, content, tokenizer, info.chat_template)
    (admitted,) = EngineRecord(instance.workdir / RECORD).with_prompt(ids)
    assert len(admitted.output_ids) < MAX_TOKENS


REFUSALS = {  # what ATOM's HTTP route says, and the prompt's words and fields
    "prompt above max_model_len": (
        "maximum context length",
        lambda limit: (limit + 1, {"max_tokens": 1}),
    ),
    "prompt past the KV pool": (
        "KV cache holds at most",
        lambda limit: (limit - 8, {"max_tokens": 1}),
    ),
    "max_tokens past max_model_len": (
        "maximum context length",
        lambda limit: (8, {"max_tokens": limit - 2}),
    ),
    "max_tokens below 1": ("at least 1", lambda limit: (8, {"max_tokens": 0})),
    "max_tokens above the cap": (
        "for this deployment",
        lambda limit: (8, {"max_tokens": limit + 1}),
    ),
    "top_p out of range": ("top_p must be", lambda limit: (8, {"top_p": 0.0})),
    "top_k out of range": ("top_k must be", lambda limit: (8, {"top_k": -2})),
}


@pytest.mark.topology("single")
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("case", REFUSALS)
def test_atomesh_refuses_with_the_status_atom_http_routes_answer(topology, route, case):
    router, instance = topology.routers["mesh"], topology.instances["single"]
    info = model_info(instance)
    says, build = REFUSALS[case]
    count, fields = build(info.max_context_length)
    request = body(route, info.served_model_name, words(count), **fields)
    status, reply = instance.post(ROUTES[route], request)
    assert status == 400 and says in reply, reply
    assert router.post(ROUTES[route], request)[0] == status


@pytest.mark.topology("single")
@pytest.mark.parametrize("stream", [False, True], ids=["unary", "stream"])
def test_a_repeated_chat_reports_the_prefix_cache_hit_the_http_chat_reports(
    topology, stream
):
    router, instance = topology.routers["mesh"], topology.instances["single"]
    info, content = model_info(instance), words(40, lead=6)
    content += " Hello" if stream else " world"  # one prompt per case
    request = body("chat", info.served_model_name, content, max_tokens=4, stream=stream)

    def details(target) -> dict | None:
        status, replies = send(target, "chat", request)
        assert status == 200, replies
        return answer("chat", replies)[2].get("prompt_tokens_details")

    details(instance)  # the prompt's full blocks enter ATOM's prefix cache
    over_http = details(instance)
    assert over_http["cached_tokens"] > 0
    assert details(router) == over_http


def test_a_router_gets_its_instances_urls_and_waits_for_each_url_at_rank(tmp_path):
    p, d = "grpc://127.0.0.1:1", "grpc://127.0.0.1:2"
    urls, ranks = {"p": p, "d": d}, {"p": 2, "d": 1}
    spec = RouterSpec("pd", prefill=["p"], decode=["d"], args=["--dp-aware"])
    pd = Atomesh(tmp_path, spec, urls, ranks, "atomesh")
    flags = ["--pd-disaggregation", "--prefill", p, "--decode", d, "--dp-aware"]
    assert pd.cmd[-6:] == flags
    assert pd.workers == [f"{p}@0", f"{p}@1", f"{d}@0"]
    regular = Atomesh(tmp_path, RouterSpec("r", ["p", "d"]), urls, ranks, "atomesh")
    assert (regular.cmd[-3:], regular.workers) == (["--worker-urls", p, d], [p, d])


@pytest.mark.parametrize(
    "router, refused",
    [
        ('name = "p"\nworkers = ["p"]', "a router repeats a name"),
        ('name = "r"\nworkers = ["q"]', "router r names no instance q"),
        ('name = "r"\nworkers = ["http"]', "router r needs gRPC of http"),
    ],
    ids=["repeated name", "unknown instance", "instance without gRPC"],
)
def test_a_router_naming_what_it_cannot_route_to_is_refused(tmp_path, router, refused):
    path = tmp_path / "refused.toml"
    instance = '[[instance]]\nname = "{}"\nmodel = "m"\nargs = []\nfake = {{}}\n'
    grpc = instance.format("p") + "grpc = true\n"
    path.write_text(f"{grpc}{instance.format('http')}[[router]]\n{router}\n")
    with pytest.raises(ValueError, match=refused):
        load(path)


def test_a_topology_with_a_router_fails_to_launch_without_atomesh_bin(
    tmp_path_factory, monkeypatch
):
    monkeypatch.delenv(ATOMESH_BIN, raising=False)
    scratch = tmp_path_factory.mktemp("unset")
    path = scratch / "unset.toml"
    path.write_text(
        f'[[instance]]\nname = "single"\nmodel = "{FIXTURE}"\nargs = []\n'
        'fake = {}\ngrpc = true\n\n[[router]]\nname = "mesh"\nworkers = ["single"]\n'
    )
    launch = Topology(path, scratch)
    with pytest.raises(RuntimeError, match=f"{ATOMESH_BIN} is unset"):
        launch.wait_ready()
    assert (launch.proc.returncode, launch.groups()) == (1, [])


def test_a_chat_renders_with_the_template_atom_serves_not_its_tokenizer_files(
    tmp_path_factory, topologies, tokenizer
):
    topologies.down()  # one topology up at a time
    scratch = tmp_path_factory.mktemp("own")  # short: ZMQ ipc sockets live in it
    (scratch / "template.jinja").write_text(OWN_TEMPLATE)
    path = scratch / "own.toml"
    path.write_text(
        f'[[instance]]\nname = "own"\nmodel = "{FIXTURE}"\n'
        f'args = ["--chat-template", "{scratch / "template.jinja"}"]\n'
        'fake = {}\ngrpc = true\n\n[[router]]\nname = "mesh"\nworkers = ["own"]\n'
    )
    launch = Topology(path, scratch)
    try:
        launch.wait_ready()
        router, instance = launch.routers["mesh"], launch.instances["own"]
        info, content = model_info(instance), words(8)
        assert info.chat_template == OWN_TEMPLATE != tokenizer.chat_template
        request = body("chat", info.served_model_name, content, max_tokens=4)
        status, reply = router.post(ROUTES["chat"], request)
        assert status == 200, reply
        model, record = info.served_model_name, instance.workdir / RECORD
        ids = rendered_ids(router, model, "chat", content, tokenizer, OWN_TEMPLATE)
        files = rendered_ids(router, model, "chat", content, tokenizer, None)
        assert ids != files
        assert [r.prompt_ids for r in EngineRecord(record).requests.values()] == [ids]
    finally:
        left = launch.stop()
    assert left == {}


def test_the_launch_command_starts_single_with_atomesh_serves_and_leaves_no_process(
    tmp_path_factory, topologies
):
    topologies.down()  # one topology up at a time
    launch = Topology(TOPOLOGIES / "single.toml", tmp_path_factory.mktemp("single"))
    try:
        launch.wait_ready()
        assert "ready: " in launch.tail()
        router, instance = launch.routers["mesh"], launch.instances["single"]
        assert instance.get("/health") == (200, {"status": "ok"})
        assert router.workers == [f"grpc://{instance.grpc}"]
        info = model_info(instance)
        request = body("chat", info.served_model_name, words(8), max_tokens=8)
        status, reply = router.post(ROUTES["chat"], request)
        assert status == 200, reply
        assert reply["usage"]["completion_tokens"] == 8
        groups = [*launch.groups(), *launch.groups("routers")]
        assert all(group_members(group) for group in groups)
    finally:
        left = launch.stop()
    assert (launch.proc.returncode, left) == (0, {})
    assert [pid for group in groups for pid in group_members(group)] == []
