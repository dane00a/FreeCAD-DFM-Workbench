# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Test parts, built from primitives rather than shipped as CAD files.

The machining rules were developed against a corpus of a couple of hundred
parts, and the corpus is what makes a change to a recognizer safe to make:
run it over all of them and see what moved. But the parts themselves are
commercial work and are not distributed, so what is kept here is the recipe
rather than the result -- every one is built from boxes, cylinders and
booleans, which is both smaller than a STEP file and considerably easier to
read when a fixture needs changing.

Each builder is registered by name and returns one solid. `verify.py` checks
the built shapes against `geometry_oracle.json`, which records face, edge and
solid counts, volume, area and bounding box for each. That is what makes
these the same parts rather than merely similar ones: a builder that drifts
by a millimetre fails against the recorded volume.

Adding one::

    @fixture("my_part")
    def build_my_part():
        return cut(box(0, 0, 0, 80, 60, 25), cylinder(40, 30, -1, 5, 30))
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Callable, Dict

from OCP.TopoDS import TopoDS_Shape


#: name -> builder, in registration order.
REGISTRY: Dict[str, Callable[[], TopoDS_Shape]] = {}


def fixture(name: str):
    """Register a builder under the name its STEP file would have had."""

    def register(builder: Callable[[], TopoDS_Shape]):
        if name in REGISTRY:
            raise ValueError(f"fixture {name!r} is already registered")
        REGISTRY[name] = builder
        return builder

    return register


def load_all() -> Dict[str, Callable[[], TopoDS_Shape]]:
    """Import every builder module so its registrations run."""
    for _, module, _ in pkgutil.iter_modules(__path__):
        if not module.startswith("_") and module != "shapes":
            importlib.import_module(f".{module}", __name__)
    return REGISTRY
