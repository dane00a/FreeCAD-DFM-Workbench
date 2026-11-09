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
import inspect
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


#: The seam-moved variants the reference corpus ships, ``base -> angles``.
#: Each is the base fixture rebuilt with every closed surface's parametric
#: seam rotated that many degrees -- the same solid, cut somewhere else --
#: and registered as ``<base>__seam<deg>``. Every variant should analyse
#: exactly as its control does; a difference is a dependence on a modelling
#: artefact. The base builder takes the angle as a keyword ``seam_deg``,
#: defaulting to 0, which must reproduce the control verbatim.
#:
#: Copied from ``all_fixture_names()`` in the reference's
#: ``tests/fixtures/fixture_generator.cpp``. The reference's unit harness
#: also builds a 73-degree arm of each that the corpus does not ship; only
#: the shipped ones are here.
SEAM_VARIANTS: Dict[str, tuple] = {
    "through_hole": (37,),
    "counterbore": (37,),
    "countersink": (37,),
    "blind_hole_drill_point": (37,),
    "thin_wall": (37,),
    "hole_intersecting": (37,),
    "inspection_test_block": (37,),
    "tall_boss": (37,),
    "shaft_endcap": (37,),
    "hydraulic_actuator_end_cap": (37,),
    "single_datum_dome": (37,),
    "knurled_grip_shaft": (37,),
    "knurl_neg_flatted_shaft": (73,),
    "turned_valley_tight_radius": (37,),
    "sm_punch_grid": (37,),
    "text_neg_decor_groove": (37,),
    "hole_breakout_into_pocket": (37,),
    "bolt_circle_4x": (37,),
    "boss_on_plate": (37,),
    "gearbox_flange": (37,),
    "sealed_void": (37,),
    "four_axis_hex_block": (37,),
    "off_cardinal_viz_block": (37,),
    "euro_lock_cylinder": (37,),
    "servo_coupling_clamp": (37,),
    "aerospace_tensioner_pulley": (37,),
    "hygienic_manifold_plate": (37,),
}


def seam_variant_name(base: str, seam_deg: float) -> str:
    return f"{base}__seam{seam_deg:g}"


def _register_seam_variants() -> None:
    """Register ``<base>__seam<deg>`` for every base that has been ported.

    A base that is not registered yet, or whose builder does not take
    ``seam_deg`` yet, is skipped rather than raising, so the corpus keeps
    loading while the variants are ported one at a time.
    """
    for base, angles in SEAM_VARIANTS.items():
        builder = REGISTRY.get(base)
        if builder is None:
            continue
        if "seam_deg" not in inspect.signature(builder).parameters:
            continue
        for angle in angles:
            name = seam_variant_name(base, angle)
            if name in REGISTRY:
                continue
            REGISTRY[name] = (
                lambda builder=builder, angle=angle: builder(seam_deg=float(angle))
            )


def load_all() -> Dict[str, Callable[[], TopoDS_Shape]]:
    """Import every builder module so its registrations run."""
    for _, module, _ in pkgutil.iter_modules(__path__):
        if not module.startswith("_") and module != "shapes":
            importlib.import_module(f".{module}", __name__)
    _register_seam_variants()
    return REGISTRY
