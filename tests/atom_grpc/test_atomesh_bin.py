# SPDX-License-Identifier: MIT
"""ATOMESH_BIN names the atomesh this tree builds, which end-to-end tests start;
without it they fail rather than skip."""

import os
import re
import subprocess
from pathlib import Path

CARGO_TOML = Path(__file__).resolve().parents[2] / "atom" / "mesh" / "Cargo.toml"


def test_atomesh_bin_runs_atomesh():
    path = os.environ.get("ATOMESH_BIN")
    assert path, "ATOMESH_BIN is unset: build atom/mesh's atomesh and name it there"
    # The first top-level `version` is the [package] table's.
    version = re.search(r'^version = "(.+)"$', CARGO_TOML.read_text(), re.MULTILINE)[1]
    run = subprocess.run(
        [path, "--version"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (run.returncode, run.stdout.strip()) == (0, f"atomesh {version}"), run.stderr
