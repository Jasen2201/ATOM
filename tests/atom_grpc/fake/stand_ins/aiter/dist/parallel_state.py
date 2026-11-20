# SPDX-License-Identifier: MIT
"""The group getters Mooncake's connector module imports; only its worker half,
which fake instances replace, calls them."""


def get_tp_group():
    raise RuntimeError("fake instances have no aiter TP group")


def get_dp_group():
    raise RuntimeError("fake instances have no aiter DP group")
