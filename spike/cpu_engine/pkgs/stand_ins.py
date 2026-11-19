"""Build a stand-in package from a table of modules and names, and log each name
a process reads to $SPIKE_NAME_LOG (process, name, kind, what, reader)."""

import enum
import multiprocessing
import os
import sys
import types

UNUSED = object()
_seen: set[tuple[str, str]] = set()


def _kind(value) -> tuple[str, str]:
    doc = (getattr(value, "__doc__", None) or "").strip().split("\n")[0]
    if isinstance(value, types.ModuleType):
        return "module", "imported"
    if isinstance(value, type) and issubclass(value, enum.Enum):
        return "enum", "members " + ", ".join(value.__members__)
    if getattr(value, "kind", None) == "queue":
        return "queue", doc
    if getattr(value, "unused", False):
        return "constant", "bound at import, never called (raises if called)"
    if isinstance(value, type):
        return "constant", doc
    if callable(value):
        return "behaviour", doc
    return "constant", repr(value)


def _reader() -> str:
    frame = sys._getframe(2)
    while frame is not None and frame.f_code.co_filename.startswith("<frozen"):
        frame = frame.f_back
    return frame.f_globals.get("__name__", "?") if frame is not None else "?"


class _Watched(types.ModuleType):
    def __getattribute__(self, attr):
        value = super().__getattribute__(attr)
        if attr.startswith("__"):
            return value
        key = f"{super().__getattribute__('__name__')}.{attr}"
        reader = _reader()
        path = os.environ.get("SPIKE_NAME_LOG")
        if (key, reader) not in _seen and path:
            _seen.add((key, reader))
            process = multiprocessing.current_process().name
            fields = (process, key, *_kind(value), reader)
            with open(path, "a") as log:
                log.write("\t".join(fields) + "\n")
        return value


def _unused(name: str):
    def placeholder(*_args, **_kwargs):
        raise NotImplementedError(f"{name} is bound at import, never called here")

    placeholder.unused = True
    return placeholder


def stand_in(root: str, table: dict[str, dict]) -> None:
    """Register `root.<sub>` for each table key ("" is the root) with its names."""
    for sub, names in table.items():
        parts = [root, *sub.split(".")] if sub else [root]
        for depth in range(2, len(parts) + 1):
            name = ".".join(parts[:depth])
            if name not in sys.modules:
                module = types.ModuleType(name)
                module.__path__ = []
                sys.modules[name] = module
                setattr(
                    sys.modules[".".join(parts[: depth - 1])], parts[depth - 1], module
                )
        module = sys.modules[".".join(parts)]
        for attr, value in names.items():
            value = _unused(f"{module.__name__}.{attr}") if value is UNUSED else value
            setattr(module, attr, value)
    for name in [m for m in sys.modules if m == root or m.startswith(root + ".")]:
        sys.modules[name].__class__ = _Watched
