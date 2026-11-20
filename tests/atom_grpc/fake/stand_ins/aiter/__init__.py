# SPDX-License-Identifier: MIT
"""Test-only aiter for fake instances: only the names their processes read."""

import enum


class QuantType(enum.Enum):
    """aiter's quantization granularity, with the members ATOM's config reads."""

    No = 0
