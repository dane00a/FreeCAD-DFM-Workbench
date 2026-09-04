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

import math

from OCP.TopoDS import TopoDS_Shape

from . import fixture
from .shapes import box, cone, cut, cylinder, fuse


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
    """7.27 mm across: no drill in any index is that size.

    Not 7.3, which is a stock metric drill. The size has to sit between the
    9/32" (7.144) and 19/64" (7.541) fractional drills and off the 0.1 mm
    metric increments, or the part stops being the thing it is named for.
    """
    return cut(plate(), cylinder(25, 25, -1, 3.635, 27.0))


# -- arrays and patterns -----------------------------------------------------


@fixture("linear_array_3x")
def build_linear_array_3x() -> TopoDS_Shape:
    """Three bores in a row, for the pattern recognizer to group."""
    shape = box(0, 0, 0, 80.0, 40.0, 15.0)
    for index in range(3):
        shape = cut(shape, cylinder(20.0 + index * 20.0, 20, -1, 3.0, 17.0))
    return shape


@fixture("grid_3x3")
def build_grid_3x3() -> TopoDS_Shape:
    shape = box(0, 0, 0, 100.0, 100.0, 15.0)
    for row in range(3):
        for column in range(3):
            shape = cut(
                shape,
                cylinder(25.0 + column * 25.0, 25.0 + row * 25.0, -1, 3.0, 17.0),
            )
    return shape


@fixture("bolt_circle_4x")
def build_bolt_circle_4x() -> TopoDS_Shape:
    """Four holes on a 60 mm pitch circle, which is a bolt circle, not a row."""
    shape = cylinder(0, 0, 0, 40.0, 10.0)
    for index in range(4):
        angle = index * math.pi / 2.0
        shape = cut(
            shape,
            cylinder(30.0 * math.cos(angle), 30.0 * math.sin(angle), -1, 3.0, 12.0),
        )
    return shape


@fixture("holes_near_edges")
def build_holes_near_edges() -> TopoDS_Shape:
    """Bores 3 mm from the outside on a 6 mm diameter: they will break out."""
    shape = box(0, 0, 0, 50.0, 50.0, 15.0)
    for x, y in ((3.0, 25.0), (47.0, 25.0), (25.0, 3.0), (25.0, 47.0)):
        shape = cut(shape, cylinder(x, y, -1, 3.0, 17.0))
    return shape


# -- proportion --------------------------------------------------------------


@fixture("deep_pocket")
def build_deep_pocket() -> TopoDS_Shape:
    """10 mm across and 50 deep: five times what the cutter wants."""
    return cut(box(0, 0, 0, 60.0, 60.0, 60.0), box(25, 25, 10, 10.0, 10.0, 55.0))


@fixture("deep_slot")
def build_deep_slot() -> TopoDS_Shape:
    return cut(box(0, 0, 0, 50.0, 50.0, 30.0), box(10, 22.5, 10, 45.0, 5.0, 25.0))


@fixture("long_slot")
def build_long_slot() -> TopoDS_Shape:
    return cut(box(0, 0, 0, 80.0, 30.0, 20.0), box(15, 12.5, 10, 50.0, 5.0, 15.0))


@fixture("high_removal")
def build_high_removal() -> TopoDS_Shape:
    """A tray with 2 mm walls: most of the billet ends up on the floor."""
    return cut(box(0, 0, 0, 50.0, 50.0, 50.0), box(2, 2, 2, 46.0, 46.0, 50.0))


@fixture("small_part")
def build_small_part() -> TopoDS_Shape:
    """A 10 mm cube: too small to hold in a vise without a fixture."""
    return box(0, 0, 0, 10.0, 10.0, 10.0)


@fixture("thin_sheet")
def build_thin_sheet() -> TopoDS_Shape:
    """A 2.9 mm plate: thin enough that holding it is the whole problem."""
    return box(0, 0, 0, 100.0, 100.0, 2.9)


# -- protrusions -------------------------------------------------------------


@fixture("boss_on_plate")
def build_boss_on_plate() -> TopoDS_Shape:
    """One round boss and one rectangular pad, so both paths get exercised."""
    plate = box(0, 0, 0, 80.0, 60.0, 5.0)
    plate = fuse(plate, cylinder(40, 30, 5, 10.0, 15.0))
    return fuse(plate, box(7.5, 25, 5, 15.0, 10.0, 12.0))


@fixture("tall_boss")
def build_tall_boss() -> TopoDS_Shape:
    """Five diameters tall: it will ring and deflect as the cutter passes."""
    return fuse(box(0, 0, 0, 60.0, 60.0, 5.0), cylinder(30, 30, 5, 4.0, 40.0))


@fixture("rib_on_plate")
def build_rib_on_plate() -> TopoDS_Shape:
    """A 1.5 mm web standing 15 mm proud: ten times its own thickness."""
    return fuse(box(0, 0, 0, 60.0, 40.0, 5.0), box(14.25, 5, 5, 1.5, 30.0, 15.0))


@fixture("interacting_features")
def build_interacting() -> TopoDS_Shape:
    """A pocket with a bore through its floor, sharing the same volume."""
    pocketed = cut(box(0, 0, 0, 50.0, 50.0, 25.0), box(10, 10, 13, 30.0, 30.0, 20.0))
    return cut(pocketed, cylinder(25, 25, -1, 4.0, 27.0))


# -- threshold pairs ---------------------------------------------------------
#
# Each of these has a twin the other side of a rule limit, and the pair is
# what stops a rule being quietly reworded into something that no longer
# fires. If the 5.9x hole starts warning, or the 6.1x one stops, the change
# was not the refactor it looked like.


@fixture("threshold_hole_5_9x")
def build_threshold_hole_5_9x() -> TopoDS_Shape:
    return cut(box(0, 0, 0, 80.0, 80.0, 59.0), cylinder(40, 40, -1, 5.0, 61.0))


@fixture("threshold_hole_6_1x")
def build_threshold_hole_6_1x() -> TopoDS_Shape:
    return cut(box(0, 0, 0, 80.0, 80.0, 61.0), cylinder(40, 40, -1, 5.0, 63.0))


@fixture("threshold_pocket_3_9x")
def build_threshold_pocket_3_9x() -> TopoDS_Shape:
    return cut(box(0, 0, 0, 70.0, 70.0, 65.0), box(27.5, 27.5, 6.5, 15.0, 15.0, 65.0))


@fixture("threshold_pocket_4_1x")
def build_threshold_pocket_4_1x() -> TopoDS_Shape:
    return cut(box(0, 0, 0, 50.0, 50.0, 47.0), box(20, 20, 6, 10.0, 10.0, 46.0))


@fixture("threshold_wall_1_6mm")
def build_threshold_wall_1_6mm() -> TopoDS_Shape:
    """Bores 11.6 mm apart on 10 mm diameters: a 1.6 mm web."""
    drilled = cut(box(0, 0, 0, 60.0, 50.0, 25.0), cylinder(19.2, 25, -1, 5.0, 27.0))
    return cut(drilled, cylinder(30.8, 25, -1, 5.0, 27.0))


@fixture("threshold_wall_1_4mm")
def build_threshold_wall_1_4mm() -> TopoDS_Shape:
    """The same bores 0.2 mm closer: a 1.4 mm web, the other side of the limit."""
    drilled = cut(box(0, 0, 0, 60.0, 50.0, 25.0), cylinder(19.3, 25, -1, 5.0, 27.0))
    return cut(drilled, cylinder(30.7, 25, -1, 5.0, 27.0))


@fixture("thin_threshold_3_1mm")
def build_thin_threshold_3_1mm() -> TopoDS_Shape:
    """3.1 mm against thin_sheet's 2.9: the pair that guards the plate limit."""
    return box(0, 0, 0, 100.0, 100.0, 3.1)
