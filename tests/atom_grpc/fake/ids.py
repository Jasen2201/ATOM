# SPDX-License-Identifier: MIT
"""The ids a fake engine emits: at output position p, F[(h + p * s) mod |F|]."""

import math
from types import SimpleNamespace

from transformers import AutoTokenizer

from atom.model_engine.prefix_hash import prompt_fingerprint
from atom.model_engine.sequence import new_token_ids


def fill_ids(tokenizer, stop_ids) -> list[int]:
    """F: the ids that are no stop id and decode alone to ASCII letters and digits."""
    fill = []
    for token_id in range(len(tokenizer)):
        text = tokenizer.decode([token_id])
        if token_id not in stop_ids and text.isascii() and text.isalnum():
            fill.append(token_id)
    return fill


def stride(n: int) -> int:
    """s: a step coprime with n, so n consecutive positions get n distinct ids."""
    s = n // 2 + 1
    while math.gcd(s, n) != 1:
        s += 1
    return s


def fingerprint(prompt) -> int:
    """h: ATOM's `prompt_fingerprint` of a prompt given as token ids."""
    tokens = new_token_ids(prompt)
    return prompt_fingerprint(
        SimpleNamespace(token_ids=tokens, num_prompt_tokens=len(tokens))
    )


class IdLaw:
    """F and s for one tokenizer; `ids` gives a request's ids by output position."""

    def __init__(self, fill: list[int]):
        if len(fill) < 2:
            raise ValueError(f"{len(fill)} fill ids cannot differ across positions")
        self.fill = fill
        self.step = stride(len(fill))

    @classmethod
    def for_model(cls, model: str, stop_ids) -> "IdLaw":
        return cls(fill_ids(AutoTokenizer.from_pretrained(model), stop_ids))

    def ids(self, h: int, start: int, count: int) -> list[int]:
        """The ids at output positions [start, start + count) of the prompt h."""
        n = len(self.fill)
        return [self.fill[(h + p * self.step) % n] for p in range(start, start + count)]
