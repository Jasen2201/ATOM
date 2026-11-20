# SPDX-License-Identifier: MIT
"""aiter's chip query, which ATOM's config makes for DCP."""


def get_gfx() -> str:
    """gfx950, MI355X's arch: the one ATOM validates DCP on."""
    return "gfx950"
