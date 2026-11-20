# SPDX-License-Identifier: MIT
"""The timing cases' shared logic: the load-average refusal, the report header, a topology
under a step law, client and law times, and a median held to its bound."""

import dataclasses
from pathlib import Path

import pytest

from tests.atom_grpc.harness.launch import load
from tests.atom_grpc.harness.topologies import TOPOLOGIES
from tests.atom_grpc.timing import tools


def test_a_run_refuses_at_a_load_average_of_32_or_more():
    assert tools.load_average("31.99 40.00 41.00 3/2048 12345\n") == 31.99
    assert not tools.refuses(31.99)
    assert tools.refuses(32.0)
    assert tools.refuses(tools.load_average("80.50 2.00 1.00 1/9 7\n"))


def test_the_header_names_the_image_the_commit_the_load_average_and_each_core_set(
    monkeypatch,
):
    monkeypatch.setenv(tools.IMAGE_ENV, "rocm/atom-dev:latest sha256:ad25")
    monkeypatch.setenv(tools.COMMIT_ENV, "283b60e4d")
    cores = {"container": [7, 4, 5, 6], "single rank 0": [5], "client": [7]}
    lines = tools.header("a2t", 12.5, cores)
    assert "image: rocm/atom-dev:latest sha256:ad25" in lines
    assert "commit: 283b60e4d" in lines
    assert any("12.50" in line for line in lines if line.startswith("load average"))
    assert lines[-3:] == [
        "cores, container: [4, 5, 6, 7]",
        "cores, single rank 0: [5]",
        "cores, client: [7]",
    ]


def test_the_single_topology_under_a_law_loads_as_itself_with_the_law(tmp_path):
    law = "/cache/compass/laws/deepseek-v4.1-flash.json.xz"
    path = tmp_path / "single.toml"
    path.write_text(tools.under_law(TOPOLOGIES / "single.toml", law))
    specs, routers = load(path)
    original, original_routers = load(TOPOLOGIES / "single.toml")
    assert routers == original_routers
    assert len(specs) == len(original)
    for spec, before in zip(specs, original):
        model = (TOPOLOGIES / before.model).resolve()
        assert Path(spec.model) == model and model.is_dir()
        assert spec.fake == {**before.fake, "law": law}
        assert dataclasses.replace(spec, model=before.model, fake=before.fake) == before


def test_client_times_take_the_first_chunks_delay_then_time_per_token_after_it():
    chunks = [(10.030, 1), (10.040, 4), (10.050, 3), (10.070, 4)]
    ttft, itl = tools.client_times(10.0, chunks)
    assert ttft == pytest.approx(0.030)
    assert itl == pytest.approx(0.040 / 11)


def test_law_times_price_through_the_first_sample_then_each_later_chunks_step():
    # A middle chunk, the final one (the first token), two decodes the client saw and
    # one more the engine ran after the request's last token.
    steps = [(0.16, False), (0.18, True), (0.008, True), (0.009, True), (0.007, True)]
    ttft, itl = tools.law_times(steps, [1, 4, 3])
    assert ttft == pytest.approx(0.34)
    assert itl == pytest.approx(0.017 / 7)
    with pytest.raises(ValueError, match="4 chunks, 2 sampling steps"):
        tools.law_times(steps[:3], [1, 4, 3, 4])


@pytest.mark.parametrize(
    "measured, error, ok",
    [
        ([1.03, 1.01, 9.0, 0.2, 1.02], 0.02, True),  # outliers move no median
        ([1.049, 1.049, 1.049, 2.0, 0.5], 0.049, True),
        ([1.06, 1.06, 1.06, 1.0, 1.0], 0.06, False),
        ([0.94, 0.94, 0.94, 0.94, 1.0], -0.06, False),
    ],
)
def test_a_median_of_five_runs_passes_within_five_percent_of_the_prediction(
    measured, error, ok
):
    verdict = tools.verdict(measured, [1.0] * 5, 0.05)
    assert verdict.predicted == 1.0
    assert verdict.measured == pytest.approx(1.0 + error)
    assert verdict.error == pytest.approx(error)
    assert verdict.ok is ok
