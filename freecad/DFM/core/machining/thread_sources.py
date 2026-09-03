# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The two ways a thread can be known about without being modelled.

A tapped hole is a plain bore at the tap drill size. The thread lives on the
drawing, not in the solid, so geometry alone can only find the rare hole
somebody troubled to cut a helix into. That leaves most real tapped holes
invisible, and the diameter is no help: a plate full of 5 mm bores is a plate
full of 5 mm bores, and reading every one of them as an M6 would put a tap
callout on the dowel holes and the clearance holes alike.

So the thread has to be *stated* somewhere, and there are exactly two places
it can be.

A native FreeCAD document states it outright. ``PartDesign::Hole`` carries
``Threaded``, a thread size, a class, a hand and a depth, because that is how
the feature is driven. Somebody sat down and said "this one is an M6x1, right
hand, tapped 12 deep". That is a declaration by the designer and it outranks
anything geometry could infer, so it is taken as fact with nothing asked.

One Hole feature is rarely one hole. Its profile sketch can hold a dozen
circles, and a pattern feature can repeat the lot round a bolt circle or down
a row, so a declaration made once has to be carried to every copy. Where the
copies land is the awkward part: a pattern almost never states a direction
outright, it points at an origin axis or a datum or an edge of the solid and
takes whatever that is aimed at. A pattern whose copies cannot be placed
takes its hole's declaration down with it. Eleven untapped holes and one
tapped one reads like an answer, and is worse than saying nothing.

An imported STEP or IGES states nothing at all -- the translator throws the
feature tree away and leaves a bag of faces. For those the workbench can only
ask. It picks out the bores whose diameter is a standard tap drill, puts them
to the user, and thereafter treats the answer as fact: a confirmed hole is
tapped, and a rejected one is a dowel hole that must never be raised again.

Those answers live on the document rather than in the preferences. They are
facts about *this* part -- this bore, on this centreline, at this size -- and
a global preference key would carry one part's answers into the next and go
stale the moment somebody edited the model.

Nothing here imports FreeCAD at module scope. Reading a document and writing
to one are the only jobs that need it, and both are penned into their own
function so the mapping, matching and bookkeeping can be tested against a
plain fake object with no FreeCAD in the room.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional, Sequence

from .aag import SurfaceType
from .features import BORE_TYPES, FeatureInstance, FeatureType
from .threads import (
    find_by_designation,
    match_major_diameter,
    match_tap_drill,
)


# -- what a feature records about where its thread came from -----------------
#
# Rules and the census read these to tell a measurement from a declaration
# from an answered question, so the spellings are part of the contract.

#: A helix was cut in the solid and measured. The oldest source, and the only
#: one that needs no document and no user.
MODELLED_HELIX = "modelled_helix"

#: A PartDesign::Hole in the document says the bore is tapped.
NATIVE_DECLARATION = "native_declaration"

#: The user was shown a tap-drill-sized bore and said yes.
USER_CONFIRMED = "user_confirmed"


# A declared hole and a recognized bore are the same hole when their
# centrelines are this nearly the same line. Deliberately the same numbers
# the helix match uses: the question is identical -- does this thing sit on
# that bore's axis -- and two different answers to it would be a bug waiting.
_AXIS_PARALLEL_DOT = 0.98
_AXIS_LINE_MM = 0.5

# Slack on the declared bore diameter. A third of a millimetre covers the
# difference between what FreeCAD computes for a thread size and what the
# final shape measures after a chamfer or a refine, and is far too tight to
# let an M5 declaration land on an M6 bore.
_DIAMETER_SLACK_MM = 0.35

# How closely two keys have to agree to be the same bore. Tighter than the
# declaration match, because this is an identity test rather than a search:
# the answer the user gave last time must land back on the hole they were
# looking at, and nowhere else.
_KEY_POSITION_MM = 0.05
_KEY_DIAMETER_MM = 0.05
_KEY_PARALLEL_DOT = 0.999

# A helix at radius nothing is numerical noise, and so is an axis direction
# of length nothing.
_MIN_LENGTH = 1e-9

# Thread series the workbench keeps tables for. A declaration in one of these
# can be named from its major diameter when the size text will not parse; a
# declaration in any other series cannot, because matching a tapered pipe
# thread or a Whitworth against a metric table would name it wrongly rather
# than leave it unnamed.
_TABLED_TYPES = frozenset({"ISOMetricProfile", "UNC"})

# How each thread type is written on a drawing, for the series the tables do
# not cover. The metric profiles say it in the size itself.
_TYPE_SUFFIX = {
    "UNC": "UNC",
    "UNF": "UNF",
    "UNEF": "UNEF",
    "NPT": "NPT",
    "BSP": "BSP",
    "BSW": "BSW",
    "BSF": "BSF",
    "ISOTyre": "ISO tyre",
}

# A metric size written with its pitch, which is every metric size FreeCAD
# emits: "M6x1", "M8x0.75". The pitch is in the text, so a fine thread the
# coarse tables have never heard of still comes out correctly pitched.
_METRIC_SIZE = re.compile(r"^M\s*([0-9]+(?:\.[0-9]+)?)\s*[xX]\s*([0-9]+(?:\.[0-9]+)?)$")

# FreeCAD writes this where a thread size would go when there is no thread.
_NO_SIZE = ("", "---", "none", "None")

# Only one of the depth types puts a number on the tapped length. "Hole
# Depth" and the DIN 76 form both mean "tapped as far as it goes", and the
# rules already worst-case an unstated tapped length to the full bore, which
# is the same statement made once instead of twice.
_DIMENSIONED_DEPTH = "Dimension"

# The property the confirmations live in, and the object that carries it.
_STORE_PROPERTY = "DFMThreadRecords"
_STORE_NAME = "DFMThreadRecord"

Point = tuple[float, float, float]


# =============================================================================
# Naming a declared thread
# =============================================================================


def _trim(value: float) -> str:
    """A dimension written the way a drawing writes it, without trailing noise."""
    return f"{value:g}"


def resolve_declared_size(
    size_text: str,
    thread_type: str = "",
    major_mm: Optional[float] = None,
) -> Optional[tuple[str, float, Optional[float]]]:
    """Name a declared thread, and give its nominal size and pitch.

    Returns the designation, the nominal diameter and the pitch, with the
    pitch left as nothing when the workbench holds no table for that series
    and the size text does not spell it out. That happens for the imperial
    fine series and for pipe threads, and an unknown pitch is the honest
    answer there: the run-out rule falls back to a diameter multiple and says
    so, which is better than quoting a coarse pitch for a fine thread.

    The order is deliberate. The size text is what the designer chose, so it
    is tried first and its pitch believed; the major diameter is only a way
    of naming a size the tables spell differently from FreeCAD.
    """
    text = (size_text or "").strip()

    if text not in _NO_SIZE:
        # A size the tables already know, spelled however FreeCAD spells it.
        # Going through the table rather than the text keeps one thread from
        # being called "M6x1" by one source and "M6x1.0" by another.
        spec = find_by_designation(text)
        if spec is not None:
            return spec.designation, spec.nominal_mm, spec.pitch_mm

        parsed = _METRIC_SIZE.match(text)
        if parsed is not None:
            nominal = float(parsed.group(1))
            pitch = float(parsed.group(2))
            if nominal > 0.0 and pitch > 0.0:
                return f"M{_trim(nominal)}x{_trim(pitch)}", nominal, pitch

    if major_mm is None or major_mm <= 0.0:
        return None

    if thread_type in _TABLED_TYPES:
        spec = match_major_diameter(major_mm)
        if spec is not None:
            return spec.designation, spec.nominal_mm, spec.pitch_mm

    # Out of table and out of parseable text. The callout is still worth
    # carrying: the rules that need a nominal diameter get it from what
    # FreeCAD computed, and the ones that need a pitch stand down.
    if text in _NO_SIZE:
        return None
    suffix = _TYPE_SUFFIX.get(thread_type, "")
    designation = f"{text} {suffix}".strip()
    return designation, major_mm, None


# =============================================================================
# Naming a bore so the answer survives a rebuild
# =============================================================================


def _normalised(direction: Sequence[float]) -> Optional[Point]:
    """A direction as a unit vector, still pointing the way it was given.

    Kept apart from ``_unit`` because a pattern cares which way round its
    axis is. Turning a bolt circle the wrong way puts every copy but the
    first somewhere it is not.
    """
    x, y, z = float(direction[0]), float(direction[1]), float(direction[2])
    length = math.sqrt(x * x + y * y + z * z)
    if length < _MIN_LENGTH:
        return None
    return (x / length, y / length, z / length)


def _unit(direction: Sequence[float]) -> Optional[Point]:
    """A direction as a unit vector pointing into a fixed half of space.

    Which way a bore's axis points is an accident of how the kernel built the
    face, and it flips between rebuilds. Forcing the sign is what stops the
    same hole being filed twice under opposite signs.
    """
    normalised = _normalised(direction)
    if normalised is None:
        return None
    x, y, z = normalised
    for component in (x, y, z):
        if abs(component) > 1e-9:
            if component < 0.0:
                return (-x, -y, -z)
            break
    return (x, y, z)


def _foot_of_axis(origin: Sequence[float], direction: Point) -> Point:
    """The one point on an axis that names the line rather than the face.

    A cylinder reports its axis through whatever point the kernel happened to
    park there, and that point slides along the line from one rebuild to the
    next. The foot of the perpendicular from the model origin does not: it is
    a property of the line itself, which is what a durable name needs.
    """
    ox, oy, oz = float(origin[0]), float(origin[1]), float(origin[2])
    along = ox * direction[0] + oy * direction[1] + oz * direction[2]
    return (
        ox - along * direction[0],
        oy - along * direction[1],
        oz - along * direction[2],
    )


@dataclass(frozen=True)
class BoreKey:
    """A bore named by where it is rather than by which face it happens to be.

    Face indices were the obvious choice and the wrong one. They come out of
    ``TopExp::MapShapes`` in whatever order the kernel walked the shape, and
    adding a fillet on the far side of the part renumbers every face after
    it. An answer filed under "Face 34" would silently move to a different
    hole; an answer filed under a centreline and a diameter stays on the hole
    it was given about, and stops applying only when somebody moves or
    resizes that hole -- which is exactly when it should stop applying.
    """

    object_name: str
    diameter_mm: float
    point: Point
    direction: Point

    def encode(self) -> str:
        return "|".join(
            (
                self.object_name,
                f"{self.diameter_mm:.3f}",
                ",".join(f"{value:.3f}" for value in self.point),
                ",".join(f"{value:.4f}" for value in self.direction),
            )
        )

    @classmethod
    def decode(cls, text: str) -> Optional["BoreKey"]:
        parts = (text or "").split("|")
        if len(parts) != 4:
            return None
        try:
            point = tuple(float(value) for value in parts[2].split(","))
            direction = tuple(float(value) for value in parts[3].split(","))
            if len(point) != 3 or len(direction) != 3:
                return None
            return cls(parts[0], float(parts[1]), point, direction)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def matches(self, other: "BoreKey") -> bool:
        if self.object_name != other.object_name:
            return False
        if abs(self.diameter_mm - other.diameter_mm) > _KEY_DIAMETER_MM:
            return False
        dot = sum(a * b for a, b in zip(self.direction, other.direction))
        if abs(dot) < _KEY_PARALLEL_DOT:
            return False
        gap = math.sqrt(sum((a - b) ** 2 for a, b in zip(self.point, other.point)))
        return gap <= _KEY_POSITION_MM


def bore_key(
    object_name: str,
    diameter_mm: float,
    origin: Sequence[float],
    direction: Sequence[float],
) -> Optional[BoreKey]:
    """Name a bore from its diameter and its centreline."""
    unit = _unit(direction)
    if unit is None or diameter_mm <= 0.0:
        return None
    return BoreKey(
        object_name=object_name or "",
        diameter_mm=float(diameter_mm),
        point=_foot_of_axis(origin, unit),
        direction=unit,
    )


# =============================================================================
# What a source says about one bore
# =============================================================================


@dataclass(frozen=True)
class ThreadFact:
    """A thread the workbench is prepared to assert, and on whose word."""

    designation: str
    nominal_mm: float
    pitch_mm: Optional[float] = None
    depth_mm: Optional[float] = None
    evidence: str = NATIVE_DECLARATION
    hand: str = "right"
    declared_by: str = ""

    def apply_to(
        self, feature: FeatureInstance, hole_depth_mm: Optional[float] = None
    ) -> None:
        """Promote a bore to a tapped hole and record where the thread came from.

        Both writes belong together. A feature typed as threaded but carrying
        no designation is one the thread rules skip in silence, which reads
        from the outside exactly like a rule that has stopped working.
        """
        feature.type = FeatureType.THREADED_HOLE
        feature.parameters["thread_designation"] = self.designation
        feature.parameters["thread_nominal_mm"] = self.nominal_mm
        if self.pitch_mm:
            feature.parameters["thread_pitch_mm"] = self.pitch_mm
        if self.depth_mm and self.depth_mm > 0.0:
            # A stated tapped length that runs past the bottom of the bore is
            # a stale property left behind by an edit, not a measurement.
            limit = (hole_depth_mm or 0.0) + 0.5
            if hole_depth_mm is None or self.depth_mm <= limit:
                feature.parameters["thread_depth_mm"] = round(self.depth_mm, 6)
        feature.parameters["thread_evidence"] = self.evidence
        if self.hand == "left":
            # A left-hand tap is a special order in most shops, so it is
            # worth saying out loud rather than leaving to be discovered.
            feature.parameters["thread_hand"] = "left"
        if self.declared_by:
            feature.parameters["thread_declared_by"] = self.declared_by


@dataclass(frozen=True)
class ThreadDeclaration:
    """One threaded ``PartDesign::Hole``, reduced to plain numbers.

    Positions is a list because one Hole feature drills as many holes as its
    profile sketch holds circles, and they are all the same thread.
    """

    designation: str
    nominal_mm: float
    pitch_mm: Optional[float]
    positions: tuple[Point, ...]
    direction: Point
    bore_window: tuple[float, float]
    depth_mm: Optional[float] = None
    hand: str = "right"
    declared_by: str = ""

    def covers(
        self, diameter_mm: float, origin: Sequence[float], direction: Sequence[float]
    ) -> bool:
        """Whether this declaration is about the bore described.

        Axis line and diameter, which is how the helix search already matches
        a modelled thread to the bore it was cut in. Nothing else is on offer:
        FreeCAD keeps no usable record of which face on the final shape came
        from which feature, so the geometry has to do the identifying.
        """
        low, high = self.bore_window
        if not low - _DIAMETER_SLACK_MM <= diameter_mm <= high + _DIAMETER_SLACK_MM:
            return False
        unit = _unit(direction)
        if unit is None:
            return False
        if abs(sum(a * b for a, b in zip(unit, self.direction))) < _AXIS_PARALLEL_DOT:
            return False
        return any(
            _distance_to_line(position, origin, unit) <= _AXIS_LINE_MM
            for position in self.positions
        )

    def as_fact(self) -> ThreadFact:
        return ThreadFact(
            designation=self.designation,
            nominal_mm=self.nominal_mm,
            pitch_mm=self.pitch_mm,
            depth_mm=self.depth_mm,
            evidence=NATIVE_DECLARATION,
            hand=self.hand,
            declared_by=self.declared_by,
        )


def _distance_to_line(
    point: Sequence[float], origin: Sequence[float], direction: Point
) -> float:
    """Perpendicular distance from a point to an axis."""
    offset = tuple(float(point[i]) - float(origin[i]) for i in range(3))
    cross = (
        offset[1] * direction[2] - offset[2] * direction[1],
        offset[2] * direction[0] - offset[0] * direction[2],
        offset[0] * direction[1] - offset[1] * direction[0],
    )
    return math.sqrt(sum(value * value for value in cross))


# =============================================================================
# Reading a PartDesign::Hole
# =============================================================================


def _flag(obj, name: str, default: bool = False) -> bool:
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return bool(value) if value is not None else default


def _text(obj, name: str, default: str = "") -> str:
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    if value is None:
        return default
    return str(value)


def _length(obj, name: str) -> Optional[float]:
    """A length property as a number, whatever wrapper it arrives in.

    FreeCAD hands back a float for ``App::PropertyLength``, but a Quantity
    turns up often enough on the same property in other versions that the
    ``Value`` case is worth carrying.
    """
    try:
        value = getattr(obj, name)
    except Exception:
        return None
    if value is None:
        return None
    if hasattr(value, "Value"):
        value = value.Value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0.0 else None


def _number(obj, name: str) -> Optional[float]:
    """A property as a number, zero and negative included.

    ``_length`` reads a diameter, where nothing-or-less means the property
    was never filled in. A pattern's length, angle and offset are different:
    a run of zero is a legal way to stack copies on top of each other, and a
    negative one is a legal way to run the pattern backwards.
    """
    try:
        value = getattr(obj, name)
    except Exception:
        return None
    if value is None:
        return None
    if hasattr(value, "Value"):
        value = value.Value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _count(obj, name: str) -> Optional[int]:
    """An occurrence count, or nothing when the property will not read."""
    try:
        value = getattr(obj, name)
    except Exception:
        return None
    if value is None:
        return None
    if hasattr(value, "Value"):
        value = value.Value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _numbers(obj, name: str) -> list[float]:
    """A float-list property, or nothing at all if any of it will not read."""
    try:
        values = getattr(obj, name)
    except Exception:
        return []
    if not values:
        return []
    found: list[float] = []
    for value in values:
        try:
            found.append(float(value))
        except (TypeError, ValueError):
            return []
    return found


def declaration_from_hole(
    hole, positions: Sequence[Point], direction: Sequence[float]
) -> Optional[ThreadDeclaration]:
    """Turn one threaded Hole feature into a declaration, or nothing.

    Duck-typed on purpose. Everything this needs is a handful of named
    properties, so nothing here has to know it is looking at a FreeCAD
    document object, and the whole mapping can be tested against a stand-in.
    """
    if not _flag(hole, "Threaded"):
        return None

    unit = _unit(direction)
    if unit is None or not positions:
        return None

    thread_type = _text(hole, "ThreadType")
    if thread_type in _NO_SIZE:
        # Threaded set with no profile chosen. The feature is half filled in
        # and there is no thread to name.
        return None

    drilled = _length(hole, "Diameter")
    major = _length(hole, "ThreadDiameter")
    resolved = resolve_declared_size(
        _text(hole, "ThreadSize"), thread_type, major or drilled
    )
    if resolved is None:
        return None
    designation, nominal, pitch = resolved

    # Which of the two diameters is the drill and which the crest depends on
    # the version and on whether the helix was modelled, and the answer does
    # not matter: the bore on the final shape is somewhere between them, so
    # the pair is taken as a window rather than a figure.
    sizes = [value for value in (drilled, major, nominal) if value]
    if not sizes:
        return None
    window = (min(sizes), max(sizes))

    depth = None
    if _text(hole, "ThreadDepthType") == _DIMENSIONED_DEPTH:
        depth = _length(hole, "ThreadDepth")

    return ThreadDeclaration(
        designation=designation,
        nominal_mm=nominal,
        pitch_mm=pitch,
        positions=tuple(
            (float(p[0]), float(p[1]), float(p[2])) for p in positions
        ),
        direction=unit,
        bore_window=window,
        depth_mm=depth,
        hand="left" if _text(hole, "ThreadDirection") == "Left" else "right",
        declared_by=_text(hole, "Label") or _text(hole, "Name"),
    )


# =============================================================================
# One hole drilled many times: the transformed features
# =============================================================================
#
# A tapped hole on a bolt circle is drawn once and repeated. FreeCAD keeps
# the repeat as a feature of its own -- a pattern, a mirror, or a stack of
# both -- and the copies exist only in the finished solid, with nothing on
# them to say which feature put them there. So the copies have to be worked
# out from the pattern's own numbers, and a declaration read off the one Hole
# feature carried to every place those numbers land.
#
# Getting this wrong is not a quiet failure. Every position in a declaration
# claims a bore, so a direction taken the wrong way round does not lose the
# copies, it puts the tap callout on whatever bores happen to lie where the
# copies were expected. That is why everything below refuses rather than
# guesses, and why a refusal takes the whole declaration with it.

#: The transform features whose copies can be placed.
_TRANSFORM_TYPES = frozenset(
    {
        "PartDesign::LinearPattern",
        "PartDesign::PolarPattern",
        "PartDesign::Mirrored",
        "PartDesign::MultiTransform",
    }
)

#: Every feature that repeats another one, the refused ones included.
#: ``PartDesign::Scaled`` is here to be recognized and turned down: a scaled
#: copy of an M6 tapped hole is a bore of some other size that no tap fits,
#: so carrying the declaration onto it would be a callout for a thread that
#: cannot be cut.
_REPEATING_TYPES = _TRANSFORM_TYPES | frozenset({"PartDesign::Scaled"})

# How a sketch numbers the axes a pattern can be aimed along. The horizontal
# and vertical axes and the normal are counted backwards from zero; anything
# from zero up is a construction line the user drew to aim at.
_SKETCH_AXES = {"H_Axis": -1, "V_Axis": -2, "N_Axis": -3}

# Only a full turn puts the last copy back on the first, so it is the one
# case where the sweep is divided by the number of copies rather than by the
# gaps between them. Half a degree either side is still a full turn.
_FULL_TURN_DEG = 360.0
_FULL_TURN_SLACK_DEG = 1e-6

# A custom spacing of less than nothing is FreeCAD's way of saying "no
# custom spacing here, use the offset".
_NO_CUSTOM_SPACING = 0.0

# Past this many copies something has gone wrong with the model rather than
# with the reading, and every position is scanned against every bore.
_MAX_COPIES = 4096

_IDENTITY_ROWS = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


@dataclass(frozen=True)
class Transform:
    """Where a pattern puts one copy of what it repeats.

    A plain 3x3 and a shift rather than a placement, because a mirror is one
    of these and no placement can hold one: a reflection turns the part
    inside out in a way a position and a rotation cannot express.
    """

    rows: tuple[tuple[float, float, float], ...] = _IDENTITY_ROWS
    offset: Point = (0.0, 0.0, 0.0)

    def apply_point(self, point: Sequence[float]) -> Point:
        return tuple(  # type: ignore[return-value]
            sum(self.rows[i][j] * float(point[j]) for j in range(3)) + self.offset[i]
            for i in range(3)
        )

    def apply_direction(self, direction: Sequence[float]) -> Point:
        # No shift: an axis says which way, not where.
        return tuple(  # type: ignore[return-value]
            sum(self.rows[i][j] * float(direction[j]) for j in range(3))
            for i in range(3)
        )

    def then(self, later: "Transform") -> "Transform":
        """This one, and then the other. The order a MultiTransform reads in."""
        return Transform(
            rows=tuple(
                tuple(
                    sum(later.rows[i][k] * self.rows[k][j] for k in range(3))
                    for j in range(3)
                )
                for i in range(3)
            ),
            offset=tuple(  # type: ignore[arg-type]
                sum(later.rows[i][k] * self.offset[k] for k in range(3))
                + later.offset[i]
                for i in range(3)
            ),
        )


def _about(rows: tuple, point: Sequence[float]) -> Point:
    """The shift that leaves a chosen point where it was.

    A rotation and a mirror are both written about something -- an axis, a
    plane -- and turn into a matrix about the origin plus this.
    """
    return tuple(  # type: ignore[return-value]
        float(point[i]) - sum(rows[i][j] * float(point[j]) for j in range(3))
        for i in range(3)
    )


def _translation(direction: Point, distance: float) -> Transform:
    return Transform(offset=tuple(value * distance for value in direction))  # type: ignore[arg-type]


def _rotation(point: Point, axis: Point, degrees: float) -> Transform:
    """A turn of so many degrees about a line through a point."""
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    x, y, z = axis
    rows = (
        (
            cos + x * x * (1.0 - cos),
            x * y * (1.0 - cos) - z * sin,
            x * z * (1.0 - cos) + y * sin,
        ),
        (
            y * x * (1.0 - cos) + z * sin,
            cos + y * y * (1.0 - cos),
            y * z * (1.0 - cos) - x * sin,
        ),
        (
            z * x * (1.0 - cos) - y * sin,
            z * y * (1.0 - cos) + x * sin,
            cos + z * z * (1.0 - cos),
        ),
    )
    return Transform(rows=rows, offset=_about(rows, point))


def _reflection(point: Point, normal: Point) -> Transform:
    """The other side of a plane through a point."""
    rows = tuple(
        tuple(
            (1.0 if i == j else 0.0) - 2.0 * normal[i] * normal[j] for j in range(3)
        )
        for i in range(3)
    )
    return Transform(rows=rows, offset=_about(rows, point))


def _spacings(obj, suffix: str, gaps: int) -> Optional[list[float]]:
    """The gap between each pair of copies, when the pattern is given by gap.

    A pattern dimensioned by spacing carries one offset for the ordinary case
    and an optional list for the awkward one, where a value of less than
    nothing means "this gap is the ordinary one after all".
    """
    offset = _number(obj, "Offset" + suffix)
    if offset is None:
        return None
    custom = _numbers(obj, "Spacings" + suffix)
    return [
        custom[index]
        if index < len(custom) and custom[index] >= _NO_CUSTOM_SPACING
        else offset
        for index in range(gaps)
    ]


def _steps_along(obj, suffix: str) -> Optional[list[float]]:
    """How far along its direction a linear pattern puts each copy.

    The first entry is always nothing, which is the original hole standing
    where it was drawn. A pattern is only ever the copies plus the thing
    copied, and the feature that made the original is upstream of the
    pattern, so the pattern's own list has to include it.
    """
    count = _count(obj, "Occurrences" + suffix)
    if count is None or count < 1 or count > _MAX_COPIES:
        return None
    if count == 1:
        return [0.0]

    if any(abs(value) > _MIN_LENGTH for value in _numbers(obj, "SpacingPattern" + suffix)):
        # A repeating run of uneven gaps. It is experimental, it is off in
        # the interface unless a hidden preference is set, and how it settles
        # against the plain spacings list is not written down anywhere. A
        # pattern using it is refused rather than read half right.
        return None

    mode = _text(obj, "Mode" + suffix, "Extent")
    if mode == "Extent":
        # Dimensioned end to end, so the length is shared between the gaps.
        total = _number(obj, "Length" + suffix)
        if total is None:
            return None
        gaps = [total / float(count - 1)] * (count - 1)
    elif mode == "Spacing":
        gaps = _spacings(obj, suffix, count - 1)  # type: ignore[assignment]
        if gaps is None:
            return None
    else:
        return None

    way = -1.0 if _flag(obj, "Reversed" + suffix) else 1.0
    steps, run = [0.0], 0.0
    for gap in gaps:
        run += gap * way
        steps.append(run)
    return steps


def _angles_around(obj) -> Optional[list[float]]:
    """The angle each copy of a polar pattern is turned to."""
    count = _count(obj, "Occurrences")
    if count is None or count < 1 or count > _MAX_COPIES:
        return None
    if count == 1:
        return [0.0]

    if any(abs(value) > _MIN_LENGTH for value in _numbers(obj, "SpacingPattern")):
        return None

    mode = _text(obj, "Mode", "Extent")
    if mode == "Extent":
        sweep = _number(obj, "Angle")
        if sweep is None:
            return None
        whole = abs(abs(sweep) - _FULL_TURN_DEG) < _FULL_TURN_SLACK_DEG
        gaps = [sweep / float(count if whole else count - 1)] * (count - 1)
    elif mode == "Spacing":
        gaps = _spacings(obj, "", count - 1)  # type: ignore[assignment]
        if gaps is None:
            return None
    else:
        return None

    way = -1.0 if _flag(obj, "Reversed") else 1.0
    angles, run = [0.0], 0.0
    for gap in gaps:
        run += gap * way
        angles.append(run)
    return angles


def transforms_of(obj, resolver) -> Optional[tuple[Transform, ...]]:
    """Every place a transform feature puts a copy, or nothing at all.

    Nothing at all is a refusal, and it is returned whenever any part of the
    feature cannot be read with certainty: an axis that will not resolve, a
    spacing scheme this does not cover, a scaled copy, a kind of transform
    that is not on the list. The caller drops the declaration rather than
    applying what could be worked out, because a declaration that reaches
    some of a bolt circle is a worse answer than one that reaches none of it.

    ``resolver`` turns the feature's links into plain numbers and is the only
    part of this that needs a live document.
    """
    if _text(obj, "TransformMode", "Features") != "Features":
        # Set to repeat the whole shape rather than the listed features. The
        # copies are fused, so a hole in one lands in solid metal in the next
        # and is filled in by it -- the bores that survive depend on how the
        # copies overlap, which is not something to work out from properties.
        return None

    type_id = _text(obj, "TypeId")

    if type_id == "PartDesign::LinearPattern":
        along = resolver.axis(getattr(obj, "Direction", None))
        steps = _steps_along(obj, "")
        if along is None or steps is None:
            return None
        copies = [_translation(along[1], step) for step in steps]
        if (_count(obj, "Occurrences2") or 1) > 1:
            # A grid. The second direction multiplies the first rather than
            # continuing it, so every copy of the row is repeated down it.
            across = resolver.axis(getattr(obj, "Direction2", None))
            down = _steps_along(obj, "2")
            if across is None or down is None:
                return None
            copies = [
                first.then(_translation(across[1], step))
                for step in down
                for first in copies
            ]
        return tuple(copies)

    if type_id == "PartDesign::PolarPattern":
        around = resolver.axis(getattr(obj, "Axis", None))
        angles = _angles_around(obj)
        if around is None or angles is None:
            return None
        return tuple(_rotation(around[0], around[1], angle) for angle in angles)

    if type_id == "PartDesign::Mirrored":
        plane = resolver.plane(getattr(obj, "MirrorPlane", None))
        if plane is None:
            return None
        # A mirror keeps the original and adds one copy, and it is the only
        # transform that does not turn its hand over: a designer mirrors a
        # plate to get the far half of a symmetric part, not to specify a
        # left-hand tap, and the bore itself is a plain drill either way.
        return (Transform(), _reflection(plane[0], plane[1]))

    if type_id == "PartDesign::MultiTransform":
        listed = list(getattr(obj, "Transformations", None) or ())
        if not listed:
            return (Transform(),)
        # Each transformation acts on everything the ones before it made, so
        # a row of two turned three ways is six holes and not five. Refusing
        # any one of them refuses the lot: a MultiTransform read down to its
        # first two stages would put copies where the finished feature has
        # none.
        stacked = [Transform()]
        for step in listed:
            found = transforms_of(step, resolver)
            if found is None:
                return None
            if len(stacked) * len(found) > _MAX_COPIES:
                return None
            stacked = [earlier.then(later) for earlier in stacked for later in found]
        return tuple(stacked)

    return None


def _copied_by(obj) -> list:
    """The features a transform feature repeats.

    A transform set to repeat the whole shape names nothing, so the answer
    there is everything cut into the part before it. Those declarations are
    on their way to being dropped rather than expanded, and dropping them
    means knowing which they are.
    """
    if _text(obj, "TransformMode", "Features") == "Features":
        return list(getattr(obj, "Originals", None) or ())

    chain: list = []
    step = getattr(obj, "BaseFeature", None)
    seen: set = set()
    while step is not None:
        name = getattr(step, "Name", None) or id(step)
        if name in seen:
            break
        seen.add(name)
        chain.append(step)
        step = getattr(step, "BaseFeature", None)
    return chain


def copies_by_feature(objects: Sequence, resolver) -> tuple[dict, set]:
    """Which features get repeated, where to, and which cannot be worked out.

    Returns the transforms that apply to each feature by name, and the names
    whose repeats were refused. A feature in the second set has no usable
    answer at all -- not the copies, and not the original either, since a
    declaration that covers the hole a pattern started from and none of the
    ones it made is the shape of answer this is here to stop.
    """
    inside: set = set()
    for obj in objects:
        for step in getattr(obj, "Transformations", None) or ():
            name = getattr(step, "Name", None)
            if name:
                # A stage of a MultiTransform. It repeats nothing on its own
                # account, and reading it as though it did would double the
                # copies it contributes.
                inside.add(name)

    copies: dict = {}
    refused: set = set()
    for obj in objects:
        if _text(obj, "TypeId") not in _REPEATING_TYPES:
            continue
        if (getattr(obj, "Name", None) or "") in inside:
            continue

        found = transforms_of(obj, resolver)
        for feature in _copied_by(obj):
            name = getattr(feature, "Name", None)
            if not name:
                continue
            if found is None or _text(feature, "TypeId") in _REPEATING_TYPES:
                # The second case is a pattern of a pattern, which is what a
                # MultiTransform is for. Reached any other way it is a shape
                # this cannot account for.
                refused.add(name)
                _warn(
                    f"{_text(obj, 'Label') or _text(obj, 'Name') or 'a pattern'} "
                    f"repeats {_text(feature, 'Label') or name} in a way the "
                    "copies cannot be placed from; any thread it declares is "
                    "left off the whole set rather than put on part of it"
                )
                continue
            copies.setdefault(name, []).extend(found)

    for name, found in list(copies.items()):
        if len(found) > _MAX_COPIES:
            refused.add(name)
            copies.pop(name)
    return copies, refused


# =============================================================================
# Where the declarations come from: the document
# =============================================================================


def _walk(root, seen: set) -> Iterator[object]:
    """Every object the target is built out of, itself included.

    Down the dependency links rather than across the document: a second body
    sitting beside the analysed one has holes of its own, and none of them are
    in this part.
    """
    if root is None:
        return
    name = getattr(root, "Name", None) or id(root)
    if name in seen:
        return
    seen.add(name)
    yield root
    for child in getattr(root, "OutList", ()) or ():
        yield from _walk(child, seen)


def _sketch_centres(sketch) -> list[Point]:
    """Where a hole profile puts its holes, in the sketch's own frame.

    The circles in the sketch are the holes; the sketch placement is only the
    plane they sit on, and is applied further up. Construction geometry is
    skipped -- it is there to drive the dimensions, not to be drilled.

    A profile that is a datum point or a face selection rather than a sketch
    leaves nothing to read, and the sketch origin stands in. That is right for
    the single-hole case and wrong for nothing, because a datum point profile
    only ever makes one hole.
    """
    centres: list[Point] = []
    facades = getattr(sketch, "GeometryFacadeList", None)
    if facades:
        pairs = [
            (facade.Geometry, bool(getattr(facade, "Construction", False)))
            for facade in facades
        ]
    else:
        geometry = getattr(sketch, "Geometry", ()) or ()
        pairs = [(item, False) for item in geometry]

    for geometry, construction in pairs:
        if construction:
            continue
        centre = getattr(geometry, "Center", None)
        radius = getattr(geometry, "Radius", None)
        if centre is None or radius is None:
            continue
        centres.append((centre.x, centre.y, centre.z))

    if not centres:
        centres.append((0.0, 0.0, 0.0))
    return centres


def _placement_transform(placement) -> Transform:
    """A FreeCAD placement as plain numbers this can compose with."""
    matrix = placement.toMatrix()
    return Transform(
        rows=(
            (matrix.A11, matrix.A12, matrix.A13),
            (matrix.A21, matrix.A22, matrix.A23),
            (matrix.A31, matrix.A32, matrix.A33),
        ),
        offset=(matrix.A14, matrix.A24, matrix.A34),
    )


def _link_parts(link) -> tuple:
    """A link property as the object it points at and the subelement named.

    ``App::PropertyLinkSub`` arrives as the object paired with a list of
    subelement names, but an empty name is common -- an origin axis is the
    whole object -- and a bare object turns up often enough to be worth
    forgiving.
    """
    if link is None:
        return None, ""
    obj, subs = link, ()
    if isinstance(link, (tuple, list)):
        if not link:
            return None, ""
        obj = link[0]
        subs = link[1] if len(link) > 1 else ()
    if isinstance(subs, str):
        subs = (subs,)
    for candidate in subs or ():
        if candidate:
            return obj, str(candidate)
    return obj, ""


class DocumentResolver:
    """What a pattern's axis and mirror plane links actually point at.

    A pattern almost never states a direction. It points at something -- an
    origin axis, one of a sketch's own axes, a datum, an edge or a face of the
    solid -- and takes whatever that is aimed at when the model rebuilds.
    Working the direction back out is most of the job, and getting it wrong is
    worse than not having it: the copies still get declared, just somewhere
    else, on whatever bores happen to be there. So anything not recognized
    below comes back as nothing, and the caller drops the declaration.

    Everything is answered in the document's global frame. FreeCAD does this
    arithmetic in the body's own frame instead, which comes to the same line
    in space, and the global frame is the one that survives the analysed shape
    being a body, a boolean of two of them, or a link to either.
    """

    def _to_global(self, obj):
        """The placement that lifts an object's own shape into world space.

        A feature's ``Shape`` already carries the feature's placement, so the
        object's own placement has to come back off before the global one goes
        on, or it is applied twice.
        """
        return obj.getGlobalPlacement().multiply(obj.Placement.inverse())

    def _sketch_axis(self, obj, sub: str) -> Optional[tuple]:
        """One of a sketch's own axes, named the way a pattern names it."""
        if not hasattr(obj, "getAxis"):
            return None
        code = _SKETCH_AXES.get(sub)
        if code is None:
            if not sub.startswith("Axis"):
                return None
            try:
                code = int(sub[4:])
            except ValueError:
                return None
        try:
            axis = obj.getAxis(code)
            placed = obj.getGlobalPlacement()
        except Exception:
            return None
        base = placed.multVec(axis.Base)
        # A construction line is whatever length the user drew it, so the
        # direction off a sketch axis is not a unit vector to start with.
        direction = placed.Rotation.multVec(axis.Direction)
        return ((base.x, base.y, base.z), (direction.x, direction.y, direction.z))

    def _shape_piece(self, obj, sub: str):
        """The bit of an object's shape a link names, or the whole of it."""
        shape = getattr(obj, "Shape", None)
        if shape is None:
            return None
        if not sub:
            return shape
        try:
            return shape.getElement(sub)
        except Exception:
            return None

    def _shape_plane(self, obj, sub: str) -> Optional[tuple]:
        """A flat face, as the plane it lies in.

        The underlying surface is read rather than the face, because which
        way round a face is turned is a fact about how the kernel built it
        and flips on a rebuild, while the surface it sits on keeps pointing
        the same way. FreeCAD aims the transform by the surface, so this
        does too.

        Anything but a flat face is turned down, which is what FreeCAD does
        as well: there is no sensible mirror plane in a cylinder.
        """
        piece = self._shape_piece(obj, sub)
        if piece is None:
            return None
        try:
            faces = list(getattr(piece, "Faces", ()) or ())
            if len(faces) != 1:
                return None
            surface = faces[0].Surface
            if surface.__class__.__name__ != "Plane":
                return None
            placed = self._to_global(obj)
            point = placed.multVec(surface.Position)
            aim = placed.Rotation.multVec(surface.Axis)
            return ((point.x, point.y, point.z), (aim.x, aim.y, aim.z))
        except Exception:
            return None

    def _shape_edge(self, obj, sub: str) -> Optional[tuple]:
        """A straight edge, as the line it runs along.

        Covers the origin axes, a datum line, and an edge of the solid picked
        off the model. The curve underneath is read rather than the edge,
        because an edge carries the direction the kernel happened to walk it
        in and a straight line does not.
        """
        piece = self._shape_piece(obj, sub)
        if piece is None:
            return None
        try:
            edges = list(getattr(piece, "Edges", ()) or ())
            if len(edges) != 1:
                return None
            curve = edges[0].Curve
            direction = getattr(curve, "Direction", None)
            if direction is None or not curve.__class__.__name__.startswith("Line"):
                return None
            placed = self._to_global(obj)
            point = placed.multVec(curve.Location)
            aim = placed.Rotation.multVec(direction)
            return ((point.x, point.y, point.z), (aim.x, aim.y, aim.z))
        except Exception:
            return None

    def _placement_aim(self, obj, local) -> Optional[tuple]:
        """The last resort: read the direction off the object's placement.

        An origin axis or plane in a document that has not been recomputed
        has no shape to read, and the placement is all there is. The local
        direction differs between the two -- an axis runs along its own X and
        a plane looks along its own Z -- so the caller says which.
        """
        import FreeCAD as App  # type: ignore

        try:
            placed = obj.getGlobalPlacement()
        except Exception:
            return None
        base = placed.Base
        aim = placed.Rotation.multVec(App.Vector(*local))
        return ((base.x, base.y, base.z), (aim.x, aim.y, aim.z))

    def axis(self, link) -> Optional[tuple]:
        """A line to repeat along or turn about: a point on it and a way."""
        obj, sub = _link_parts(link)
        if obj is None:
            return None
        # A flat face counts here as well as an edge: picking a face aims the
        # pattern along its normal, which is how a user says "away from that
        # wall" without an edge to hand.
        found = (
            self._sketch_axis(obj, sub)
            or self._shape_edge(obj, sub)
            or self._shape_plane(obj, sub)
        )
        if found is None and _text(obj, "TypeId") == "App::Line":
            found = self._placement_aim(obj, (1.0, 0.0, 0.0))
        if found is None:
            return None
        direction = _normalised(found[1])
        return None if direction is None else (found[0], direction)

    def plane(self, link) -> Optional[tuple]:
        """A plane to mirror in: a point on it and its normal."""
        import FreeCAD as App  # type: ignore

        obj, sub = _link_parts(link)
        if obj is None:
            return None

        found = None
        axis = self._sketch_axis(obj, sub)
        if axis is not None:
            # A sketch axis names a plane rather than lying in one, and the
            # plane it names stands up out of the sketch: it holds the axis
            # and the sketch's own normal, so mirroring in it swings the
            # copies across the sketch rather than out of it.
            try:
                sheet = obj.getGlobalPlacement().Rotation.multVec(
                    App.Vector(0.0, 0.0, 1.0)
                )
            except Exception:
                return None
            along = axis[1]
            normal = (
                along[1] * sheet.z - along[2] * sheet.y,
                along[2] * sheet.x - along[0] * sheet.z,
                along[0] * sheet.y - along[1] * sheet.x,
            )
            found = (axis[0], normal)

        if found is None:
            found = self._shape_plane(obj, sub)
        if found is None and _text(obj, "TypeId") == "App::Plane":
            found = self._placement_aim(obj, (0.0, 0.0, 1.0))
        if found is None:
            return None
        normal = _normalised(found[1])
        return None if normal is None else (found[0], normal)


def _hole_frames(target) -> Iterator[tuple[object, list[Point], Point]]:
    """Every threaded Hole in the target, with all its holes placed and aimed.

    The placement arithmetic is the fiddly part. The shape the analysis runs
    on carries the target's own placement, while the features inside a body
    are drawn in the body's own frame, so the sketch has to be brought back
    through the global placement of both to land in the same coordinates the
    bores were measured in. Getting that wrong does not fail loudly -- it
    simply matches nothing, and the declarations go quietly unused.

    A repeated Hole comes back more than once when its copies do not all point
    the same way, which a polar pattern about anything but the drilling axis
    will do. Splitting them is not a nicety: a declaration carries one
    direction, and a set of positions on axes that disagree would match every
    bore near any of them.
    """
    objects = list(_walk(target, set()))
    copies, refused = copies_by_feature(objects, DocumentResolver())

    for obj in objects:
        if getattr(obj, "TypeId", "") != "PartDesign::Hole":
            continue
        if not _flag(obj, "Threaded"):
            continue
        if (getattr(obj, "Name", None) or "") in refused:
            continue

        profile = getattr(obj, "Profile", None)
        sketch = profile[0] if isinstance(profile, (tuple, list)) and profile else profile
        if sketch is None:
            continue

        repeats = tuple(copies.get(getattr(obj, "Name", None) or "", ())) or (Transform(),)
        centres = _sketch_centres(sketch)

        try:
            outer = _placement_transform(
                target.Placement.multiply(target.getGlobalPlacement().inverse())
            )
            plane = _placement_transform(sketch.getGlobalPlacement())
        except Exception:
            if len(repeats) > 1:
                # No global placements means no way to put the copies in the
                # frame the bores were measured in, and the original alone is
                # the half answer this is here not to give.
                continue
            outer = Transform()
            plane = _placement_transform(target.Placement.multiply(sketch.Placement))

        # Grouped by which way the copies point, and keyed on the canonical
        # direction so that a mirrored copy standing on the same axis the
        # other way up files with the ones it belongs to.
        aimed: dict = {}
        for repeat in repeats:
            placed = plane.then(repeat).then(outer)
            aim = _unit(placed.apply_direction((0.0, 0.0, 1.0)))
            if aim is None:
                continue
            aimed.setdefault(tuple(round(value, 9) for value in aim), []).extend(
                placed.apply_point(centre) for centre in centres
            )

        for aim, positions in aimed.items():
            yield obj, positions, aim


def native_declarations(target, frames: Optional[Callable] = None) -> list[ThreadDeclaration]:
    """Every thread the document states outright for this part.

    ``frames`` is the seam. It supplies the Hole features already placed and
    aimed, which is the only part of this that needs a live document, so a
    test can hand over three tuples and exercise the rest.
    """
    if target is None:
        return []
    source = frames or _hole_frames
    found: list[ThreadDeclaration] = []
    try:
        for hole, positions, direction in source(target):
            declaration = declaration_from_hole(hole, positions, direction)
            if declaration is not None:
                found.append(declaration)
    except Exception as exc:  # a document oddity must not lose the analysis
        _warn(f"could not read the threaded holes on this part: {exc}")
    return found


def _warn(message: str) -> None:
    try:
        import FreeCAD as App  # type: ignore

        App.Console.PrintWarning(f"DFM: {message}\n")
    except Exception:
        pass


# =============================================================================
# The confirmations, and where they live
# =============================================================================


@dataclass(frozen=True)
class Confirmation:
    """One answered question about one bore."""

    key: BoreKey
    accepted: bool
    designation: str = ""

    def encode(self) -> str:
        verdict = "yes" if self.accepted else "no"
        return ";".join((verdict, self.designation, self.key.encode()))

    @classmethod
    def decode(cls, text: str) -> Optional["Confirmation"]:
        parts = (text or "").split(";", 2)
        if len(parts) != 3 or parts[0] not in ("yes", "no"):
            return None
        key = BoreKey.decode(parts[2])
        if key is None:
            return None
        return cls(key=key, accepted=parts[0] == "yes", designation=parts[1])


class ConfirmationStore:
    """The answers already given about this document's bores.

    A rejection is kept as carefully as a confirmation. Being asked twice
    about the same dowel hole is the fastest way to teach somebody to click
    past the question without reading it.
    """

    def __init__(self, records: Optional[Sequence[Confirmation]] = None):
        self.records: list[Confirmation] = list(records or ())

    @classmethod
    def decode(cls, entries: Sequence[str]) -> "ConfirmationStore":
        records = [Confirmation.decode(entry) for entry in entries or ()]
        return cls([record for record in records if record is not None])

    def encode(self) -> list[str]:
        return [record.encode() for record in self.records]

    def verdict_for(self, key: Optional[BoreKey]) -> Optional[Confirmation]:
        if key is None:
            return None
        for record in self.records:
            if record.key.matches(key):
                return record
        return None

    def remember(self, confirmation: Confirmation) -> None:
        """File an answer, replacing any earlier answer about the same bore."""
        self.records = [
            record
            for record in self.records
            if not record.key.matches(confirmation.key)
        ]
        self.records.append(confirmation)

    def __len__(self) -> int:
        return len(self.records)


def _find_store(document):
    """The document's answer sheet, if it has one.

    Found by the property rather than the name, because FreeCAD renames an
    object whose name is taken and the property is what actually identifies
    it.
    """
    for obj in getattr(document, "Objects", ()) or ():
        if hasattr(obj, _STORE_PROPERTY):
            return obj
    return None


def load_confirmations(document) -> ConfirmationStore:
    """Read the answers off a document, forgiving a document that has none."""
    if document is None:
        return ConfirmationStore()
    try:
        store = _find_store(document)
        if store is None:
            return ConfirmationStore()
        return ConfirmationStore.decode(list(getattr(store, _STORE_PROPERTY, ()) or ()))
    except Exception as exc:
        _warn(f"could not read the confirmed threads on this document: {exc}")
        return ConfirmationStore()


def save_confirmations(document, store: ConfirmationStore) -> bool:
    """Write the answers onto the document, making the holder if need be.

    An ``App::FeaturePython`` with no Proxy, hidden in the tree. No Proxy is
    the point: nothing has to be importable for the document to open, so a
    part that has been through here still loads on a machine with no DFM
    addon installed, and the answers are still sitting there when it comes
    back. Properties bolted onto the user's own Body would have been simpler
    and worse -- editing a body's properties dirties it, and a shape that
    recomputes because somebody answered a question about a hole is a
    surprise nobody asked for.
    """
    if document is None:
        return False
    try:
        holder = _find_store(document)
        if holder is None:
            holder = document.addObject("App::FeaturePython", _STORE_NAME)
            holder.Label = "DFM Thread Record"
            holder.addProperty(
                "App::PropertyStringList",
                _STORE_PROPERTY,
                "DFM",
                "Bores the user has confirmed or rejected as tapped.",
            )
            view = getattr(holder, "ViewObject", None)
            if view is not None:
                view.Visibility = False
        setattr(holder, _STORE_PROPERTY, store.encode())
        return True
    except Exception as exc:
        _warn(f"could not save the confirmed threads on this document: {exc}")
        return False


# =============================================================================
# The evidence, gathered
# =============================================================================


@dataclass
class ThreadEvidence:
    """Everything that can be said about this part's threads without measuring."""

    object_name: str = ""
    declarations: tuple[ThreadDeclaration, ...] = ()
    confirmations: ConfirmationStore = field(default_factory=ConfirmationStore)

    def key_for(
        self,
        diameter_mm: float,
        origin: Sequence[float],
        direction: Sequence[float],
    ) -> Optional[BoreKey]:
        return bore_key(self.object_name, diameter_mm, origin, direction)

    def fact_for(
        self,
        diameter_mm: float,
        origin: Sequence[float],
        direction: Sequence[float],
    ) -> Optional[ThreadFact]:
        """The thread stated for this bore, by whoever stated it.

        The document is asked first. A declaration is the designer saying
        what the part is, and no answer a user clicked through in a dialog
        outranks that.
        """
        for declaration in self.declarations:
            if declaration.covers(diameter_mm, origin, direction):
                return declaration.as_fact()

        record = self.confirmations.verdict_for(
            self.key_for(diameter_mm, origin, direction)
        )
        if record is None or not record.accepted:
            return None
        spec = find_by_designation(record.designation)
        if spec is None:
            return None
        return ThreadFact(
            designation=spec.designation,
            nominal_mm=spec.nominal_mm,
            pitch_mm=spec.pitch_mm,
            evidence=USER_CONFIRMED,
        )

    def __bool__(self) -> bool:
        return bool(self.declarations) or bool(len(self.confirmations))


def thread_evidence_for(target, frames: Optional[Callable] = None) -> ThreadEvidence:
    """Everything stated about this part's threads. The one way in.

    Safe on nothing at all, which is the headless and the imported case both:
    a part with no document behind it simply has no evidence, and the helix
    search remains the only source, exactly as before.
    """
    if target is None:
        return ThreadEvidence()
    return ThreadEvidence(
        object_name=getattr(target, "Name", "") or "",
        declarations=tuple(native_declarations(target, frames)),
        confirmations=load_confirmations(getattr(target, "Document", None)),
    )


# =============================================================================
# Asking about the rest
# =============================================================================


@dataclass(frozen=True)
class ThreadCandidate:
    """A bore that might be tapped, put up for an answer."""

    instance_id: str
    key: BoreKey
    faces: tuple[int, ...]
    diameter_mm: float
    designation: str
    nominal_mm: float
    pitch_mm: float
    depth_mm: Optional[float] = None
    is_through: bool = False

    def describe(self) -> str:
        """The bore as a machinist would read it off the part."""
        kind = "through" if self.is_through else "blind"
        text = f"{self.diameter_mm:.2f} mm {kind} bore"
        if self.depth_mm and not self.is_through:
            text += f", {self.depth_mm:.1f} mm deep"
        return text


def centreline_of(node) -> Optional[tuple[Point, Point]]:
    """A turned face's axis as plain numbers: a point on it and a direction."""
    axis = getattr(node, "cyl_cone_axis", None)
    if axis is None:
        return None
    try:
        location, direction = axis.Location(), axis.Direction()
    except Exception:
        return None
    return (
        (location.X(), location.Y(), location.Z()),
        (direction.X(), direction.Y(), direction.Z()),
    )


def bore_wall(graph, feature: FeatureInstance):
    """The cylindrical face a tap would run in.

    The smallest internal cylinder the feature owns, so that on a counterbore
    this is the bore rather than the enlarged mouth.
    """
    best = None
    for face_id in sorted(feature.faces):
        if not graph.has_node(face_id):
            continue
        node = graph.node(face_id)
        if node.surface_type is not SurfaceType.CYLINDER:
            continue
        if node.cyl_cone_axis is None or not node.is_internal:
            continue
        if best is None or node.cyl_radius < best.cyl_radius:
            best = node
    return best


def candidates_for(
    features: Sequence[FeatureInstance],
    graph,
    evidence: Optional[ThreadEvidence] = None,
    unit_system: str = "both",
) -> list[ThreadCandidate]:
    """The bores worth asking about, and nothing else.

    A tap drill match is the only reason to raise the question and no reason
    at all to answer it, which is why this returns candidates rather than
    threads. Anything already settled is left out: a bore the document
    declares, a bore with a helix in it, and a bore somebody has already
    given a verdict on, whichever way that verdict went.
    """
    # Not `evidence or ThreadEvidence()`: evidence with nothing to say is
    # falsy, and it still carries the object name every key is filed under.
    if evidence is None:
        evidence = ThreadEvidence()
    found: list[ThreadCandidate] = []

    for feature in features:
        if feature.type not in BORE_TYPES:
            continue
        if feature.param("thread_evidence") or feature.param("thread_designation"):
            continue
        if feature.param("terminates_in_cavity"):
            continue
        diameter = feature.number("diameter_mm") or 0.0
        if diameter <= 0.0:
            continue
        spec = match_tap_drill(diameter, unit_system)
        if spec is None:
            continue

        wall = bore_wall(graph, feature)
        line = centreline_of(wall) if wall is not None else None
        if line is None:
            continue
        key = evidence.key_for(diameter, line[0], line[1])
        if key is None:
            continue
        if evidence.confirmations.verdict_for(key) is not None:
            continue
        if any(
            declaration.covers(diameter, line[0], line[1])
            for declaration in evidence.declarations
        ):
            continue

        found.append(
            ThreadCandidate(
                instance_id=feature.instance_id,
                key=key,
                faces=tuple(feature.faces),
                diameter_mm=diameter,
                designation=spec.designation,
                nominal_mm=spec.nominal_mm,
                pitch_mm=spec.pitch_mm,
                depth_mm=feature.number("depth_mm"),
                is_through=bool(feature.param("is_through")),
            )
        )

    return found


def record_answers(
    store: ConfirmationStore,
    candidates: Sequence[ThreadCandidate],
    answers: dict[str, bool],
) -> int:
    """File the verdicts and say how many bores were called tapped.

    The count is what tells the caller whether the analysis has to run again.
    A rejection changes no finding -- the workbench was not going to assert
    that thread anyway -- so a screen full of rejections is worth saving and
    not worth re-analysing for.
    """
    confirmed = 0
    for candidate in candidates:
        verdict = answers.get(candidate.key.encode())
        if verdict is None:
            continue
        store.remember(
            Confirmation(
                key=candidate.key,
                accepted=bool(verdict),
                designation=candidate.designation if verdict else "",
            )
        )
        if verdict:
            confirmed += 1
    return confirmed
