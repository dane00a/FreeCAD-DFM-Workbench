# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Standard thread sizes, and how to recognize one from geometry.

A tapped hole is not modelled as a thread. It is modelled as a plain bore at
the tap drill diameter, and the thread exists only on the drawing. So the way
to find one is to notice that a bore's diameter is suspiciously close to a
standard tap drill -- 5.0 mm is an M6, 4.2 mm is an M5 -- and say so.

That inference is a guess, and it is labelled as one. A 5 mm bore might be a
5 mm bore. The workbench asks rather than asserts: the finding says what it
thinks the hole is and offers the user the chance to confirm or deny it, and
a confirmed thread is thereafter treated as fact.

The tables are ISO metric coarse and UNC. Fine pitches are deliberately left
out: including them makes almost every drill size ambiguous, and coarse is the
default a shop reaches for absent a callout saying otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ThreadSpec:
    """One standard thread size."""

    designation: str
    tap_drill_mm: float
    nominal_mm: float
    pitch_mm: float
    system: str  # "metric" or "imperial"


#: ISO metric coarse.
METRIC_THREADS: tuple[ThreadSpec, ...] = (
    ThreadSpec("M2x0.4", 1.6, 2.0, 0.4, "metric"),
    ThreadSpec("M2.5x0.45", 2.05, 2.5, 0.45, "metric"),
    ThreadSpec("M3x0.5", 2.5, 3.0, 0.5, "metric"),
    ThreadSpec("M4x0.7", 3.3, 4.0, 0.7, "metric"),
    ThreadSpec("M5x0.8", 4.2, 5.0, 0.8, "metric"),
    ThreadSpec("M6x1.0", 5.0, 6.0, 1.0, "metric"),
    ThreadSpec("M8x1.25", 6.8, 8.0, 1.25, "metric"),
    ThreadSpec("M10x1.5", 8.5, 10.0, 1.5, "metric"),
    ThreadSpec("M12x1.75", 10.2, 12.0, 1.75, "metric"),
    ThreadSpec("M14x2.0", 12.0, 14.0, 2.0, "metric"),
    ThreadSpec("M16x2.0", 14.0, 16.0, 2.0, "metric"),
    ThreadSpec("M18x2.5", 15.5, 18.0, 2.5, "metric"),
    ThreadSpec("M20x2.5", 17.5, 20.0, 2.5, "metric"),
    ThreadSpec("M22x2.5", 19.5, 22.0, 2.5, "metric"),
    ThreadSpec("M24x3.0", 21.0, 24.0, 3.0, "metric"),
    ThreadSpec("M27x3.0", 24.0, 27.0, 3.0, "metric"),
    ThreadSpec("M30x3.5", 26.5, 30.0, 3.5, "metric"),
)

#: Unified coarse. Tap drills are the standard 75% engagement choices.
IMPERIAL_THREADS: tuple[ThreadSpec, ...] = (
    ThreadSpec("#2-56 UNC", 1.854, 2.184, 0.454, "imperial"),  # #50
    ThreadSpec("#4-40 UNC", 2.261, 2.845, 0.635, "imperial"),  # #43
    ThreadSpec("#6-32 UNC", 2.705, 3.505, 0.794, "imperial"),  # #36
    ThreadSpec("#8-32 UNC", 3.454, 4.166, 0.794, "imperial"),  # #29
    ThreadSpec("#10-24 UNC", 3.797, 4.826, 1.058, "imperial"),  # #25
    ThreadSpec("1/4-20 UNC", 5.105, 6.35, 1.27, "imperial"),  # #7
    ThreadSpec("5/16-18 UNC", 6.527, 7.938, 1.411, "imperial"),  # F
    ThreadSpec("3/8-16 UNC", 7.938, 9.525, 1.588, "imperial"),  # 5/16
    ThreadSpec("7/16-14 UNC", 9.347, 11.112, 1.814, "imperial"),  # U
    ThreadSpec("1/2-13 UNC", 10.795, 12.7, 1.954, "imperial"),  # 27/64
    ThreadSpec("5/8-11 UNC", 13.386, 15.875, 2.309, "imperial"),  # 17/32
    ThreadSpec("3/4-10 UNC", 16.510, 19.05, 2.54, "imperial"),  # 21/32
    ThreadSpec('1"-8 UNC', 22.225, 25.4, 3.175, "imperial"),  # 7/8
)

ALL_THREADS: tuple[ThreadSpec, ...] = METRIC_THREADS + IMPERIAL_THREADS

_SUFFIXES = ("UNEF", "UNF", "UNC", "UN")


def threads_for(unit_system: str) -> tuple[ThreadSpec, ...]:
    """The table to search for a shop working in one unit system.

    A metric shop that keeps no UNC taps should never be told its 5.1 mm hole
    is a quarter-twenty.
    """
    if unit_system == "metric":
        return METRIC_THREADS
    if unit_system == "imperial":
        return IMPERIAL_THREADS
    return ALL_THREADS


def match_tap_drill(
    diameter_mm: float,
    unit_system: str = "both",
    tolerance_mm: float = 0.1,
) -> Optional[ThreadSpec]:
    """The standard thread whose tap drill this diameter most nearly is.

    Nearest match within the tolerance, or nothing. The tolerance is tight on
    purpose: a hole half a millimetre off a tap drill is a hole, and guessing
    at it would put a thread callout on every clearance bore on the part.
    """
    best: Optional[ThreadSpec] = None
    best_delta = float("inf")
    for spec in threads_for(unit_system):
        delta = abs(spec.tap_drill_mm - diameter_mm)
        if delta < best_delta:
            best_delta, best = delta, spec
    if best is not None and best_delta <= tolerance_mm:
        return best
    return None


def match_major_diameter(
    diameter_mm: float,
    unit_system: str = "both",
    tolerance_mm: float = 0.2,
) -> Optional[ThreadSpec]:
    """The standard thread whose nominal size this outside diameter matches.

    For external threads, where the modelled cylinder is the major diameter
    rather than a drill. The tolerance is looser than the tap drill's because
    a modelled OD is usually cut back a little for the thread to run on.
    """
    best: Optional[ThreadSpec] = None
    best_delta = float("inf")
    for spec in threads_for(unit_system):
        delta = abs(spec.nominal_mm - diameter_mm)
        if delta < best_delta:
            best_delta, best = delta, spec
    if best is not None and best_delta <= tolerance_mm:
        return best
    return None


# -- designations ------------------------------------------------------------


def _canonical(designation: str) -> str:
    """Squash a designation to a form two spellings of it will share.

    Callouts arrive written every way a person might write them -- "M6x1.0",
    "M6 x 1", "m6X1" -- and all of them mean the same thread.
    """
    text = re.sub(r"\s+", "", designation).upper()
    # A trailing ".0" carries no information: M6X1.0 and M6X1 are one thread.
    return re.sub(r"\.0(?![0-9])", "", text)


def _without_suffix(text: str) -> str:
    for suffix in _SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def find_by_designation(designation: str) -> Optional[ThreadSpec]:
    """Resolve a written callout to a standard thread.

    Tolerant by design, because this is fed by whatever the user typed. A
    bare "M6" resolves to the coarse pitch, which is the unambiguous default
    for metric ISO; a bare "1/4-20" resolves with or without its UNC suffix.
    """
    if not designation:
        return None
    needle = _canonical(designation)
    needle_bare = _without_suffix(needle)
    # A pitchless metric callout takes the coarse pitch. Only metric: an
    # imperial size without its thread count is genuinely ambiguous.
    pitchless_metric = (
        needle.startswith("M") and "X" not in needle and "-" not in needle
    )

    for spec in ALL_THREADS:
        haystack = _canonical(spec.designation)
        if haystack == needle:
            return spec
        haystack_bare = _without_suffix(haystack)
        if haystack_bare in (needle, needle_bare):
            return spec
        if pitchless_metric and "X" in haystack:
            if haystack.split("X", 1)[0] == needle:
                return spec
    return None
