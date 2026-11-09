# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Prismatic fixtures added to the reference corpus after September 2026.

Each one exists to pin down a single behaviour that an earlier fixture let
slip through for an unrelated reason, so the docstrings say what the part
guards as well as what it is.
"""

from __future__ import annotations

from OCP.TopoDS import TopoDS_Shape

from . import fixture
from .shapes import box, cut, cylinder


@fixture("grid_with_outliers")
def build_grid_with_outliers() -> TopoDS_Shape:
    """A 4x4 grid of Ø6 holes, plus two strays of the same size and axis.

    Holes are grouped by type, diameter and axis before a pattern is looked
    for, and all eighteen here match on all three. A grid test that demands
    every member of the group sit on the lattice therefore rejects the whole
    group and reports a plainly visible 4x4 as no pattern at all. The strays
    are deliberately off the lattice in both axes and far enough from it that
    no tolerance can absorb them, and both clear the edge-distance threshold
    so nothing else fires. Expected: one grid PATTERN of 16, rows 4 by cols 4,
    with the two strays left outside it.
    """
    part = box(0, 0, 0, 120.0, 120.0, 15.0)
    for row in range(4):
        for col in range(4):
            part = cut(part, cylinder(20.0 + col * 25.0, 20.0 + row * 25.0, -1.0, 3.0, 17.0))
    part = cut(part, cylinder(108.0, 60.0, -1.0, 3.0, 17.0))
    part = cut(part, cylinder(60.0, 108.0, -1.0, 3.0, 17.0))
    return part


@fixture("counterbore_stack_plate")
def build_counterbore_stack_plate() -> TopoDS_Shape:
    """Four counterbore stations, each testing that the seat is what is measured.

    A counterbore's seat is wider than its bore, so whenever a rule asks how
    close a hole comes to something, the seat is the answer. A rule reading
    only the bore diameter calls every station here comfortable.

    - A (30, 60): three levels, Ø6 through with Ø12 and Ø20 seats, nothing
      nearby. One COUNTERBORE with a two-entry stack and no findings: the
      negative control for B and C.
    - B (80, 11): a Ø20 seat 1 mm from the Y=0 face, over a bore 8 mm clear
      of it. ``hole_edge_distance`` on the seat.
    - C (115, 45) and (115, 66): Ø20 seats 21 mm apart leave a 1 mm web
      where the bores leave 15. ``hole_web_thickness`` on the seats.
    - D (30, 25): a through bore seated at both ends, socket head one side
      and nut seat the other. One double-ended COUNTERBORE.
    """
    part = box(0, 0, 0, 140.0, 90.0, 30.0)

    def bore(x: float, y: float, z: float, radius: float, height: float) -> None:
        nonlocal part
        part = cut(part, cylinder(x, y, z, radius, height))

    # A -- the widest seat is cut last, so each step survives in the result.
    bore(30.0, 60.0, -0.1, 3.0, 30.2)
    bore(30.0, 60.0, 22.0, 6.0, 8.2)
    bore(30.0, 60.0, 25.0, 10.0, 5.2)

    # B -- the seat runs out of wall; the bore does not.
    bore(80.0, 11.0, -0.1, 3.0, 30.2)
    bore(80.0, 11.0, 25.0, 10.0, 5.2)

    # C -- 21 - 10 - 10 = 1 mm between seats, 21 - 3 - 3 = 15 between bores.
    for y in (45.0, 66.0):
        bore(115.0, y, -0.1, 3.0, 30.2)
        bore(115.0, y, 25.0, 10.0, 5.2)

    # D -- one through bore, a seat at each end.
    bore(30.0, 25.0, -0.1, 4.0, 30.2)
    bore(30.0, 25.0, 26.0, 8.0, 4.2)
    bore(30.0, 25.0, -0.1, 8.0, 4.2)

    return part
