# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The primitive test parts: one feature each, on a 50 x 50 x 25 block.

These are the parts a recognizer is written against before it ever sees a
real one. Each carries exactly one thing, so when a test fails there is no
question about which feature caused it.

The dimensions are not arbitrary. A hole 10 mm across in a 25 mm plate is
2.5 diameters deep, which is ordinary; the deep-hole part is the same hole
in a plate thick enough to be a problem. Several parts come in pairs that
straddle a threshold from either side, and those pairs are what stop a rule
being quietly reworded into something that no longer fires.
"""

from OCP.TopoDS import TopoDS_Shape

from . import fixture
from .shapes import box, cone, cut, cylinder


PLATE = (0.0, 0.0, 0.0, 50.0, 50.0, 25.0)


def plate() -> TopoDS_Shape:
    """The block everything in this module is cut from."""
    return box(*PLATE)


@fixture("simple_box")
def build_simple_box() -> TopoDS_Shape:
    """Nothing at all. The control: no rule should say anything about it."""
    return plate()


@fixture("through_hole")
def build_through_hole() -> TopoDS_Shape:
    # The drill runs past both faces so it pierces cleanly rather than
    # leaving a coincident-face mess at the surface.
    return cut(plate(), cylinder(25, 25, -1, 5.0, 27.0))


@fixture("blind_hole")
def build_blind_hole() -> TopoDS_Shape:
    # Bottoming at z=10 leaves a flat floor, which is the interesting part:
    # a drill does not make one, so the flat-bottom rule has something to say.
    return cut(plate(), cylinder(25, 25, 10, 5.0, 20.0))


@fixture("counterbore")
def build_counterbore() -> TopoDS_Shape:
    stepped = cut(plate(), cylinder(25, 25, 20, 7.0, 10.0))
    return cut(stepped, cylinder(25, 25, 5, 4.0, 25.0))


@fixture("countersink")
def build_countersink() -> TopoDS_Shape:
    # A 90 degree included angle: the cone widens from 3 to 8 over 5 mm.
    sunk = cut(plate(), cone(25, 25, 20, 3.0, 8.0, 5.0))
    return cut(sunk, cylinder(25, 25, 5, 3.0, 25.0))


@fixture("rectangular_pocket")
def build_rect_pocket() -> TopoDS_Shape:
    return cut(plate(), box(10, 10, 5, 30.0, 30.0, 25.0))


@fixture("l_slot")
def build_l_slot() -> TopoDS_Shape:
    """A slot open at one end, which is what makes it a slot and not a pocket."""
    return cut(plate(), box(10, 20, 13, 45.0, 10.0, 15.0))


@fixture("step_feature")
def build_step_feature() -> TopoDS_Shape:
    """Half the plate faced down by 10 mm."""
    return cut(plate(), box(25, 0, 15, 30.0, 50.0, 20.0))


@fixture("thin_wall")
def build_thin_wall() -> TopoDS_Shape:
    """Two bores 11 mm apart on 10 mm diameters: a 1 mm web between them."""
    drilled = cut(box(0, 0, 0, 60.0, 50.0, 25.0), cylinder(20, 25, -1, 5.0, 27.0))
    return cut(drilled, cylinder(31, 25, -1, 5.0, 27.0))


@fixture("deep_hole")
def build_deep_hole() -> TopoDS_Shape:
    """A 6 mm drill through 50 mm of plate.

    Eight diameters deep: past the point where a drill needs pecking and
    starts to wander off line.
    """
    return cut(box(0, 0, 0, 50.0, 50.0, 50.0), cylinder(25, 25, -1, 3.0, 52.0))


@fixture("nonstandard_hole")
def build_nonstandard_hole() -> TopoDS_Shape:
    """7.3 mm across: no drill in any index is that size."""
    return cut(plate(), cylinder(25, 25, -1, 3.65, 27.0))
