# SPDX-License-Identifier: MIT
"""A test marked `@pytest.mark.topology(name)` gets `topologies/<name>.toml` running as
`topology`; tests are grouped by topology, so each starts once, one at a time."""

import pytest

from tests.atom_grpc.harness.topologies import Topologies, by_topology


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "topology(name): runs on tests/atom_grpc/topologies/<name>.toml"
    )


def pytest_collection_modifyitems(items):
    items[:] = by_topology(items)


@pytest.fixture(scope="session")
def topologies(tmp_path_factory):
    topologies = Topologies(tmp_path_factory)
    yield topologies
    topologies.down()


@pytest.fixture
def topology(request, topologies):
    marker = request.node.get_closest_marker("topology")
    if marker is None:
        pytest.fail("the `topology` fixture needs @pytest.mark.topology(name)")
    return topologies.up(marker.args[0])
