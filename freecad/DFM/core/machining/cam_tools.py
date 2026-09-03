# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The shop's real tooling, read out of FreeCAD's CAM workbench.

A shop that programs in CAM has already typed its cutters in once -- every
diameter, every flute length, every corner radius, sitting in Tools/Bit as
its own file. Asking for them again on the Tooling page buys nothing but a
second copy to go stale, and the copy that goes stale is always the one the
analysis reads.

So read CAM instead. What comes back is the same ``ToolEntry`` the rest of
the workbench works in, which means every rule that asks "is there a cutter
for this" starts asking about cutters somebody can actually pick up.

Three things are worth knowing before trusting it.

CAM holds more kinds of bit than any rule here knows how to ask about. A
V-bit, a slitting saw, a dovetail cutter and a probe are all perfectly good
tools that no machining rule consults, and quietly filing them under "end
mill" would put a 90-degree engraving point on the shelf as a 6 mm cutter
and pass a pocket corner nothing can cut. Those are counted and left on the
bench, never mapped to the nearest thing.

CAM also holds *less* than the shelf does. It is a milling and drilling
workbench: there is no boring bar and no turning insert anywhere in its
vocabulary. Its silence about lathe tooling is not the shop saying it owns
none, so the catalogue's lathe entries stay alongside whatever CAM gives.

And a CAM install nobody has set up has nothing in it at all, which has to
land back on the catalogue. An empty shelf is worse than a generic one:
every tool-dependent rule stands down when it finds no tool, and does it
silently, so the analysis comes back clean because it asked nothing.

Nothing here imports FreeCAD at module scope. Only the collector at the
bottom needs a live CAM; everything above it works on plain records, so the
mapping can be exercised with no FreeCAD in sight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .config import ToolEntry, default_tool_library


# =============================================================================
# What CAM calls a tool, and what this workbench calls it
# =============================================================================

# CAM's shape name, folded to bare letters, against the type a rule asks for.
# A bullnose is a corner-radius end mill and is read as one, radius and all;
# a tapered ball nose still finishes with a ball of the stated diameter, which
# is the only thing the freeform rules ask a ball nose about.
SHAPE_TYPES = {
    "endmill": "end_mill",
    "bullnose": "end_mill",
    "torus": "end_mill",  # CAM's own alias for a bullnose
    "ballend": "ball_nose",
    "taperedballnose": "ball_nose",
    "drill": "drill",
    "reamer": "reamer",
    "tap": "tap",
}

# Bits CAM can hold that nothing here knows how to ask about. Each is a real
# tool and none of them is an end mill; a shop is told how many were passed
# over rather than finding them silently absent.
UNMAPPED_SHAPES = {
    "chamfer": "chamfer bit",
    "vbit": "V-bit",
    "dovetail": "dovetail cutter",
    "radius": "corner-rounding cutter",
    "fillet": "corner-rounding cutter",
    "slittingsaw": "slitting saw",
    "threadmill": "thread mill",
    "probe": "probe",
    "custom": "custom shape",
}

# The other half of the mismatch: types the shelf carries that CAM cannot
# express at all. These come off the catalogue whenever CAM is the source,
# because a shop that turns every day would otherwise lose the turning rules
# by connecting a milling workbench.
LATHE_TYPES = ("boring_bar", "turning_insert")

# The only properties worth pulling off a bit. CAM records a dozen more --
# shank diameter, flute count, tip angle -- and no rule asks about any of them.
READ_PROPERTIES = (
    "Diameter",
    "CornerRadius",
    "CuttingEdgeHeight",
    "CuttingEdgeLength",
    "Length",
)


# =============================================================================
# Records
# =============================================================================


@dataclass
class CamBit:
    """One CAM tool bit, cut down to what the shelf cares about.

    This is the seam. A live CAM install fills these in from real tool bits;
    a test fills them in by hand. Either way the mapping below is the same
    code, which is the only way any of it gets exercised without FreeCAD.
    """

    shape: str  # CAM's shape name, e.g. "Endmill"
    label: str = ""
    properties: dict = field(default_factory=dict)


@dataclass
class CamLibrary:
    """One of CAM's tool libraries: a name and the bits in it."""

    label: str
    bits: list = field(default_factory=list)


@dataclass
class CamReading:
    """What CAM had to say, and what had to be left on the bench.

    The counts matter as much as the tools. A shop connecting CAM and getting
    four cutters back needs to know whether the other nine were engraving
    bits or whether the read fell over half way.
    """

    tools: list = field(default_factory=list)
    libraries: list = field(default_factory=list)  # library labels, in order
    bits_seen: int = 0
    skipped: dict = field(default_factory=dict)  # what it was -> how many
    error: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.tools)


# =============================================================================
# Reading a value
# =============================================================================

_NUMBER = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")

_LENGTH_UNITS = {
    "": 1.0,
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": 25.4,
    "inch": 25.4,
    '"': 25.4,
    "ft": 304.8,
    "'": 304.8,
}


def _parse_length(text: str) -> Optional[float]:
    """A stored parameter such as ``5.0000 mm`` or ``0.375 "``, in millimetres.

    A tool bit file keeps the unit it was typed in, and a shop working in
    inches has bits written in inches. Reading the number and dropping the
    unit would put a 3/8 inch tap on the shelf as a 0.375 mm one, which is
    not a tool at all -- so an unrecognised unit is refused rather than
    assumed to be millimetres.
    """
    match = _NUMBER.search(text or "")
    if match is None:
        return None
    try:
        number = float(match.group())
    except ValueError:
        return None
    unit = text[match.end() :].strip().lower()
    factor = _LENGTH_UNITS.get(unit)
    return number * factor if factor is not None else None


def _millimetres(value) -> Optional[float]:
    """One CAM property in millimetres, whatever form it arrived in."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    # A FreeCAD Quantity. Ask it to convert rather than reading its printed
    # form: that follows whichever unit schema the user happens to have set,
    # and the shelf is stored in millimetres regardless.
    converter = getattr(value, "getValueAs", None)
    if converter is not None:
        try:
            converted = converter("mm")
            return float(getattr(converted, "Value", converted))
        except Exception:
            pass

    scalar = getattr(value, "Value", None)
    if isinstance(scalar, (int, float)) and not isinstance(scalar, bool):
        return float(scalar)

    return _parse_length(str(value))


def _fold(name: str) -> str:
    """A shape name reduced to bare lower-case letters.

    CAM writes the same shape half a dozen ways -- ``Endmill`` from the class,
    ``tap`` from a hand-written bit file, ``slitting-saw`` and ``v-bit`` from
    its own alias lists -- and none of that is a different tool.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# =============================================================================
# Mapping a bit onto the shelf
# =============================================================================


def entry_from_bit(bit: CamBit) -> Optional[ToolEntry]:
    """One CAM bit as a shelf entry, or None if no rule could use it.

    A bit with no diameter is refused along with the shapes nothing asks
    about. Zero would read as an infinitely fine cutter and let every corner
    in the part pass.
    """
    tool_type = SHAPE_TYPES.get(_fold(bit.shape))
    if tool_type is None:
        return None

    diameter = _millimetres(bit.properties.get("Diameter"))
    if not diameter or diameter <= 0.0:
        return None

    # CAM records a cutting length for everything ground along its flank and
    # nothing for a drill, which carries only an overall length. Where it says
    # nothing the flute stays zero: the reach rules pass over a tool with no
    # flute length rather than guess at one, which is the right answer for a
    # drill -- no rule asks a drill how deep it reaches anyway.
    flute = _millimetres(bit.properties.get("CuttingEdgeHeight"))
    if flute is None:
        flute = _millimetres(bit.properties.get("CuttingEdgeLength"))
    overall = _millimetres(bit.properties.get("Length"))

    corner = 0.0
    if tool_type == "ball_nose":
        # The workbench marks a full ball tip as half the diameter, the same
        # way the catalogue does.
        corner = diameter / 2.0
    elif _fold(bit.shape) in ("bullnose", "torus"):
        corner = min(_millimetres(bit.properties.get("CornerRadius")) or 0.0, diameter / 2.0)

    return ToolEntry(
        type=tool_type,
        min_diameter_mm=diameter,
        max_diameter_mm=diameter,
        corner_radius_mm=max(corner, 0.0),
        max_flute_length_mm=max(flute or 0.0, 0.0),
        max_reach_mm=max(overall or 0.0, flute or 0.0),
        # Deliberately blank. These are tools on a real shelf, not catalogue
        # sizes: a 3/8 inch end mill cuts a metric pocket perfectly well, and
        # a shop that owns one should not have it filtered out of a size match
        # because it set the workbench to metric.
        unit="",
    )


def read_bits(libraries: Sequence[CamLibrary]) -> CamReading:
    """Every library's bits as shelf entries, with a tally of what was left.

    The same cutter usually appears in several libraries -- a shop keeps one
    per machine and the 6 mm end mill goes in all of them -- so identical
    entries collapse. Duplicates would not change any verdict, but they make
    the count on the preferences page a lie.
    """
    reading = CamReading()
    seen: set = set()

    for library in libraries:
        reading.libraries.append(library.label or "unnamed")
        for bit in library.bits:
            reading.bits_seen += 1
            entry = entry_from_bit(bit)
            if entry is None:
                what = UNMAPPED_SHAPES.get(_fold(bit.shape), "unreadable bit")
                reading.skipped[what] = reading.skipped.get(what, 0) + 1
                continue
            spec = entry.to_spec()
            if spec in seen:
                continue
            seen.add(spec)
            reading.tools.append(entry)

    return reading


def lathe_tooling() -> list[ToolEntry]:
    """The catalogue's lathe entries, which CAM has no way to hold.

    CAM knows nothing about a boring bar or a turning insert, so reading it
    can only ever describe the mill. Dropping the lathe would take the
    turning rules down with it on a shop that does both.
    """
    return [tool for tool in default_tool_library() if tool.type in LATHE_TYPES]


# =============================================================================
# The live read
# =============================================================================


def cam_libraries() -> list[CamLibrary]:
    """Whatever tool libraries the shop has set up in CAM.

    Only the local store is asked. CAM ships a Default library of sample
    bits and falls back to it for anything the user has not got, which is
    right for cutting a part and wrong here: thirteen demonstration cutters
    are not an inventory, and reading them would replace the workbench's own
    catalogue with a shorter, more confident version of the same guess.
    """
    from Path.Tool import camassets  # type: ignore

    manager = camassets.cam_assets
    libraries: list[CamLibrary] = []

    for uri in manager.list_assets(asset_type="toolbitlibrary", store="local"):
        try:
            library = manager.get(uri)
        except Exception:
            # One unreadable library file must not cost the rest of the shelf.
            continue
        bits = [_record(bit) for bit in library.get_bits()]
        libraries.append(CamLibrary(label=str(getattr(library, "label", "")), bits=bits))

    return libraries


def _record(bit) -> CamBit:
    """A live CAM tool bit reduced to the record the mapping reads."""
    properties = {}
    for name in READ_PROPERTIES:
        try:
            value = bit.get_property(name)
        except Exception:
            continue  # a shape that has no such property, which is most of them
        if value is not None:
            properties[name] = value

    try:
        shape = bit.get_shape_name()
    except Exception:
        shape = ""

    return CamBit(
        shape=str(shape),
        label=str(getattr(bit, "label", "")),
        properties=properties,
    )


def read_cam_tools(source: Optional[Callable[[], Sequence[CamLibrary]]] = None) -> CamReading:
    """Read CAM, or report why not. Never raises.

    A shop opening the preferences page has not asked to be shown a
    traceback, and a CAM that is missing, half-installed or newer than this
    reader is exactly the same situation as a CAM with no tools in it: the
    catalogue answers instead.
    """
    collect = source or cam_libraries
    try:
        libraries = list(collect())
    except ImportError:
        return CamReading(error="the CAM workbench is not available")
    except Exception as exc:
        return CamReading(error=f"CAM could not be read ({exc})")
    return read_bits(libraries)


def cam_tool_library(
    source: Optional[Callable[[], Sequence[CamLibrary]]] = None,
) -> list[ToolEntry]:
    """CAM's tooling plus the lathe, or an empty list if CAM had nothing.

    Empty is the caller's signal to fall back. Deciding that here would hide
    the difference between a shop whose CAM is full and one whose CAM has
    never been opened, and the preferences page has to be able to say which.
    """
    reading = read_cam_tools(source)
    if not reading.usable:
        return []
    return list(reading.tools) + lathe_tooling()


# =============================================================================
# Saying what happened
# =============================================================================


def describe(reading: CamReading) -> str:
    """One sentence for the preferences page about what CAM gave up.

    Worth spelling out rather than showing a count: a shop that connects CAM
    and sees fewer tools than it owns needs to know whether the missing ones
    were engraving bits nothing asks about or whether the read failed.
    """
    if reading.error:
        return f"CAM was not read: {reading.error}. The catalogue is in force instead."

    if not reading.tools:
        if not reading.libraries:
            return (
                "No tool libraries set up in CAM, so there is nothing to read. "
                "The catalogue is in force instead."
            )
        return (
            f"CAM has {_count(len(reading.libraries), 'library', 'libraries')} but "
            "nothing in them a machining rule can use. The catalogue is in "
            "force instead."
        )

    parts = [
        f"Read {_count(len(reading.tools), 'tool')} from "
        f"{_count(len(reading.libraries), 'CAM library', 'CAM libraries')} "
        f"({', '.join(reading.libraries)})."
    ]
    if reading.skipped:
        listed = ", ".join(
            f"{count} {what}{'' if count == 1 else 's'}"
            for what, count in sorted(reading.skipped.items())
        )
        parts.append(f"Passed over {listed}: no machining rule asks about them.")
    parts.append(
        "Boring bars and turning inserts come off the catalogue -- CAM has no "
        "way to hold them."
    )
    return " ".join(parts)


def _count(number: int, singular: str, plural: str = "") -> str:
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"
