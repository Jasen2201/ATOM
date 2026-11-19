"""Test-only stand-in for triton: the names ATOM's CPU path and torch's own
import read once any `triton` is importable."""

from stand_ins import stand_in


def jit(fn=None, **_options):
    """Decorator returning the function unchanged; no kernel launches here."""
    return fn if fn is not None else (lambda f: f)


def constexpr(value=None):
    """`tl.constexpr` as annotation or call: returns its argument."""
    return value


class dtype:
    """A type for torch._dynamo's constant-type set; never instantiated."""


stand_in(
    __name__,
    {
        "": {"jit": jit},
        "language": {"constexpr": constexpr, "dtype": dtype},
        "backends.compiler": {},
        "compiler.compiler": {},
    },
)
