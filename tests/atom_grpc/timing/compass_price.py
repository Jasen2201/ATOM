# SPDX-License-Identifier: MIT
"""`python compass_price.py <law> <record> <out.json>`, jgong5/ATOM on PYTHONPATH: each
step of an engine record priced by Compass's `StepLinearBackend`, and the law's hops."""

import json
import lzma
import sys

from atom.compass.backends.shape import BatchView, RequestShape
from atom.compass.backends.step_linear import StepLinearBackend, StepLinearLaw
from atom.compass.backends.step_linear_fit import HOPS


def main() -> None:
    law, record, out = sys.argv[1:]
    with lzma.open(law, "rt") as f:
        spec = json.load(f)
    backend = StepLinearBackend(StepLinearLaw(spec))
    with open(record) as f:
        text = f.read()
    prices = []
    for line in text[: text.rfind("\n") + 1].splitlines():  # whole lines, as read back
        step = json.loads(line)
        if step["kind"] != "step":
            continue
        rows = (RequestShape(q, c, decode) for _, q, c, decode in step["requests"])
        view = BatchView(
            tuple(rows), capture_rung=step["rung"], produces_output=step["samples"]
        )
        prices.append(backend.price(view).seconds)
    hops = {hop: spec["host"]["hops"][hop] for hop in HOPS}  # a request's host path
    with open(out, "w") as f:
        json.dump({"steps": prices, "hops": hops}, f)


if __name__ == "__main__":
    main()
