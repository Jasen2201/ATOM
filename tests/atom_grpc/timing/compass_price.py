# SPDX-License-Identifier: MIT
"""`python compass_price.py <law> <record> <out.json>`, jgong5/ATOM on PYTHONPATH: each
step of an engine record priced by Compass's `StepLinearBackend`, in order."""

import json
import lzma
import sys

from atom.compass.backends.shape import BatchView, RequestShape
from atom.compass.backends.step_linear import StepLinearBackend, StepLinearLaw


def main() -> None:
    law, record, out = sys.argv[1:]
    with lzma.open(law, "rt") as f:
        backend = StepLinearBackend(StepLinearLaw(json.load(f)))
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
    with open(out, "w") as f:
        json.dump(prices, f)


if __name__ == "__main__":
    main()
