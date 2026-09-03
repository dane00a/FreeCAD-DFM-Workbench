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


def _unit(direction: Sequence[float]) -> Optional[Point]:
    """A direction as a unit vector pointing into a fixed half of space.

    Which way a bore's axis points is an accident of how the kernel built the
    face, and it flips between rebuilds. Forcing the sign is what stops the
    same hole being filed twice under opposite signs.
    """
    x, y, z = float(direction[0]), float(direction[1]), float(direction[2])
    length = math.sqrt(x * x + y * y + z * z)
    if length < _MIN_LENGTH:
        return None
    x, y, z = x / length, y / length, z / length
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


def _sketch_positions(sketch, frame) -> list[Point]:
    """Where a hole profile puts its holes, in the analysed shape's frame.

    The circles in the sketch are the holes; the sketch placement is only the
    plane they sit on. Construction geometry is skipped -- it is there to
    drive the dimensions, not to be drilled.

    A profile that is a datum point or a face selection rather than a sketch
    leaves nothing to read, and the frame origin stands in. That is right for
    the single-hole case and wrong for nothing, because a datum point profile
    only ever makes one hole.
    """
    import FreeCAD as App  # type: ignore

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
        placed = frame.multVec(App.Vector(centre.x, centre.y, centre.z))
        centres.append((placed.x, placed.y, placed.z))

    if not centres:
        base = frame.Base
        centres.append((base.x, base.y, base.z))
    return centres


def _hole_frames(target) -> Iterator[tuple[object, list[Point], Point]]:
    """Every threaded Hole in the target, with its holes placed and aimed.

    The placement arithmetic is the fiddly part. The shape the analysis runs
    on carries the target's own placement, while the features inside a body
    are drawn in the body's own frame, so the sketch has to be brought back
    through the global placement of both to land in the same coordinates the
    bores were measured in. Getting that wrong does not fail loudly -- it
    simply matches nothing, and the declarations go quietly unused.
    """
    import FreeCAD as App  # type: ignore

    seen: set = set()
    for obj in _walk(target, seen):
        if getattr(obj, "TypeId", "") != "PartDesign::Hole":
            continue
        if not _flag(obj, "Threaded"):
            continue

        profile = getattr(obj, "Profile", None)
        sketch = profile[0] if isinstance(profile, (tuple, list)) and profile else profile
        if sketch is None:
            continue

        try:
            inner = target.getGlobalPlacement().inverse().multiply(
                sketch.getGlobalPlacement()
            )
            frame = target.Placement.multiply(inner)
        except Exception:
            frame = target.Placement.multiply(sketch.Placement)

        normal = frame.Rotation.multVec(App.Vector(0.0, 0.0, 1.0))
        yield obj, _sketch_positions(sketch, frame), (normal.x, normal.y, normal.z)


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
