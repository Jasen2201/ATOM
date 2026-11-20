# SPDX-License-Identifier: MIT
"""Step prices: jgong5/ATOM@600fde97's `StepLinearBackend.price` in the vector forms
of its DeepSeek-V4.1-Flash MI355X law, and the constant law. Imports no `atom`."""

import json
import lzma
from typing import NamedTuple

# Each vector's features in Compass's order; those past the required ones are optional.
PREFILL = (
    "step",
    "tokens",
    "query_square",
    "query_cached",
    "history",
    "seqs",
    "out",
    "padding",
)
DECODE = ("step", "context", "graph_padding")
PER_REQUEST = ("step", "seqs")  # the draft's propose and draft-KV passes
UNPRICED = ("short_propose", "forward")  # law forms this port refuses


class Shape(NamedTuple):
    """A step as a law prices it: (query tokens, context tokens, decode) per request,
    the capture rung its decode replays (None for none) and whether it samples."""

    requests: tuple
    rung: int | None
    samples: bool


class Vector:
    """One kind's coefficients in feature order, and a padded prefill's token buckets."""

    def __init__(self, name: str, spec: dict, features: tuple, required: int):
        coefficients = spec["coefficients"]
        names = [f for f in features if f in coefficients]
        if set(coefficients) - set(names) or names[:required] != [*features[:required]]:
            holds = sorted(coefficients)
            raise ValueError(
                f"the {name} vector holds {holds}; this port prices {features}"
            )
        self.terms = [(f, float(coefficients[f])) for f in names]
        self.buckets = sorted(int(b) for b in spec.get("buckets") or ())
        if "padding" in names and not self.buckets:
            raise ValueError(f"the {name} vector pads to no buckets")

    def price(self, counts: dict) -> list[float]:
        return [counts[feature] * c for feature, c in self.terms]


class StepLaw:
    """A law file's prefill vector, decode vectors by rung ("eager" for none) and the
    draft's own law, whose terms follow the target's and its draft-KV pass's."""

    def __init__(self, law: dict, name: str = ""):
        if unpriced := [form for form in UNPRICED if form in law]:
            raise ValueError(f"the {name}law holds {unpriced}, which this port refuses")
        self.name = name
        self.prefill = law.get("prefill") and Vector(
            f"{name}prefill", law["prefill"], PREFILL, 4
        )
        self.decode = {
            None if rung == "eager" else int(rung): Vector(
                f"{name}decode rung {rung}", spec, DECODE, 3
            )
            for rung, spec in law["decode"].items()
        }
        self.propose = law.get("propose") and Vector(
            f"{name}propose", law["propose"], PER_REQUEST, 2
        )
        self.draft_kv = law.get("draft_kv") and Vector(
            f"{name}draft_kv", law["draft_kv"], PER_REQUEST, 2
        )
        self.output_prefill_kv = bool(law.get("output_prefill_kv"))
        self.draft = law.get("draft") and StepLaw(law["draft"], "draft ")

    @classmethod
    def load(cls, path: str) -> "StepLaw":
        with (lzma.open if path.endswith(".xz") else open)(path, "rt") as f:
            return cls(json.load(f))

    def price(self, shape: Shape) -> float:
        """The step's seconds, its terms folded left in Compass's order, which `sum`'s
        compensated float sum would not reproduce bit for bit."""
        terms, kv = self._terms(shape)
        drafts = []
        if self.draft:
            drafts, draft_kv = self.draft._terms(shape)
            kv += draft_kv
        total = 0.0
        for seconds in terms + kv + drafts:
            total += seconds
        return total

    def _terms(self, shape: Shape) -> tuple[list, list]:
        """This law's terms for `shape` and, apart, its draft-KV pass's: the target's."""
        rows = [(q, c) for q, c, decode in shape.requests if not decode]
        if rows and len(rows) < len(shape.requests):
            raise ValueError("a step mixing prefill and decode rows")
        per_request = {"step": 1, "seqs": len(shape.requests)}
        if not rows:
            vector = self.decode.get(shape.rung)
            if vector is None:
                rung = "eager" if shape.rung is None else shape.rung
                raise ValueError(f"no {self.name}decode vector for rung {rung}")
            context = [c for _, c, _ in shape.requests]
            padding = shape.rung * max(context) - sum(context) if shape.rung else 0
            counts = {"step": 1, "context": sum(context), "graph_padding": padding}
            kv = self.draft_kv.price(per_request) if self.draft_kv else []
            return vector.price(counts), kv
        if not self.prefill:
            raise ValueError(f"no {self.name}prefill vector")
        tokens, samples = sum(q for q, _ in rows), shape.samples
        counts = {
            "step": 1,
            "tokens": tokens,
            "query_square": sum(q * q for q, _ in rows),
            "query_cached": sum(q * (c - q) for q, c in rows),
            "history": sum(c for _, c in rows) - tokens,
            "seqs": len(rows) if samples else 0,
            "out": int(samples),
            "padding": next(
                (b - tokens for b in self.prefill.buckets if b >= tokens), 0
            ),
        }
        terms, kv = self.prefill.price(counts), []
        if self.output_prefill_kv and samples:
            terms, kv = [], terms
        if self.propose and samples:
            terms += self.propose.price(per_request)
        return terms, kv


class ConstantLaw:
    """The constant-step law: every step costs `seconds`."""

    def __init__(self, seconds: float):
        self.seconds = seconds

    def price(self, shape: Shape) -> float:
        return self.seconds
