"""collab as the tests see it when it is a package: `mod.X` as on the single file.

On the built file a test's `mod` is the loaded module. On the package it is a Package: its attributes are
the package's modules (`mod.store`, for `seam(mod, "store.write_json", …)`), and any other name is read
from the one module that holds it — at the moment of reading, not copied at the start. So the tests'
reads (`mod.read_messages()`, `mod.room().paths`, `mod.TYPED["x"] = …` on the owner's own dict) run
unchanged on both.

Nothing is set through it: a replacement goes through seam() to the module that owns the name; an
assignment here would reach no caller. A name in no module, or held by two as different objects,
is an error, not a guess: the build keeps one name to one definition, a test must not see otherwise."""

import importlib
import os
import pkgutil
import sys
import weakref
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

from _seam import seam


def package_folder() -> str:
    """The folder the suite takes the package from, or "" for the file (COLLAB_TEST_PACKAGE: 1 = src/)."""
    where = os.environ.get("COLLAB_TEST_PACKAGE", "")
    return str(Path(__file__).resolve().parent.parent / "src") if where == "1" else where


# Said once, at the start of the run: a run meant for the package that quietly ran the file is seen in its log.
if package_folder():
    print(f"package: {package_folder()}", file=sys.stderr, flush=True)


class Package:
    def __init__(self, package: ModuleType):
        modules = {
            info.name: importlib.import_module(f"{package.__name__}.{info.name}")
            for info in pkgutil.iter_modules(package.__path__)
        }
        object.__setattr__(self, "_modules", modules)
        # seam() is strict on a package: «module.name» with no such module is an error, not skipped.
        object.__setattr__(self, "__path__", package.__path__)
        object.__setattr__(self, "__name__", package.__name__)

    def __getattr__(self, name: str):
        modules = object.__getattribute__(self, "_modules")
        if name in modules:
            return modules[name]
        held = {key: vars(m)[name] for key, m in modules.items() if name in vars(m)}
        if not held:
            raise AttributeError(f"{name!r} is in no module of the package")
        values = list(held.values())
        # The same object in several (an imported os, Path): one thing, read it.
        if any(v is not values[0] for v in values[1:]):
            raise AttributeError(f"{name!r} is a different thing in {', '.join(sorted(held))}: say which module")
        return values[0]

    def __setattr__(self, name: str, value) -> None:
        raise AttributeError(f"set {name!r} in its module through seam(), not on the package")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{name!r} is not deleted through the package")


class Loader:
    """What the tests load collab with — `_Loader(name, str(COLLAB)).load_module()`, 140 places, unchanged
    Without COLLAB_TEST_PACKAGE: the file, as SourceFileLoader always did. With it — 1 for the
    repository's src/, or a folder holding a package named collab: the package imported afresh for each load
    (each test gets a fresh one and shares no TYPED, room() or lock with another), seen through Package.
    A subprocess test runs the built file either way: that is what people run."""

    def __init__(self, name: str, path: str):
        self.name, self.path = name, path

    def load_module(self):
        where = package_folder()
        if not where:
            return SourceFileLoader(self.name, self.path).load_module()
        for key in [k for k in sys.modules if k == "collab" or k.startswith("collab.")]:
            del sys.modules[key]
        if where in sys.path:
            sys.path.remove(where)
        sys.path.insert(0, where)
        facade = Package(importlib.import_module("collab"))
        # The package tests exercise source functions, but subprocesses and safe copies use the build.
        # Keep that executable boundary explicit; boot.py alone is not the distributed program.
        if "boot" in object.__getattribute__(facade, "_modules"):
            executable = seam(facade, "boot.SELF", str(Path(self.path).resolve()))
            executable.start()
            weakref.finalize(facade, executable.stop)
        return facade
