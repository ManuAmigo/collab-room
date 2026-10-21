"""The one way a test replaces a piece of the outside world in collab (tmux, other
programs, time, finding Python, opening and replacing room files): `seam(mod, "tmux", fake)`.

`mod` is collab as a test loaded it. The name may carry its module, `seam(mod, "tmuxio.tmux", …)`:
on the package the replacement goes to that module, where every caller looks it up (`tmuxio.tmux`);
on the single file the module part is skipped — everything is one module there. So a test written
once runs on the package and on the built file alike.

Only `mock.patch.object` without `create`: a replacement of a name that is not there
— renamed or moved — fails at once instead of quietly adding an attribute nobody calls."""

import dataclasses
from unittest import mock


def owner(mod, dotted: str):
    """The object holding the last part of `dotted`, and that part."""
    *modules, name = dotted.split(".")
    target = mod
    for part in modules:
        inner = getattr(target, part, None)
        # The single file has no `tmuxio` in it; a stdlib module (`os.replace`) is gone into as well.
        if inner is not None:
            target = inner
        elif hasattr(mod, "__path__"):
            # On the package a missing module is renamed or moved: skipped, a same name at the root
            # would be replaced and the callers in the moved module keep the real one.
            raise AttributeError(f"seam: no module {part!r} in {mod.__name__} — renamed or moved?")
    return target, name


def seam(mod, dotted: str, new):
    """A context manager (or decorator) replacing `dotted` in collab with `new` for its duration."""
    target, name = owner(mod, dotted)
    if not hasattr(target, name):
        raise AttributeError(f"seam: {dotted} is not in {getattr(target, '__name__', target)!r} — renamed or moved?")
    return mock.patch.object(target, name, new)


def seam_paths(mod, **fields):
    """Install paths changed for the test: the frozen set is copied with `fields` and put in
    place of the current one; the patch puts the old one back. A field not in the set is a TypeError."""
    return mock.patch.object(mod.install(), "paths", dataclasses.replace(mod.paths(), **fields))


def seam_room(mod, **fields):
    """The same for the room's paths: room().paths."""
    return mock.patch.object(mod.room(), "paths", dataclasses.replace(mod.room().paths, **fields))
