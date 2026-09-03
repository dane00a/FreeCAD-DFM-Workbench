# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2025 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Criticality(Enum):
    CRITICAL = 0
    HIGH = 1
    MEDIUM = 2
    LOW = 3

    @property
    def label(self) -> str:
        return self.name.capitalize()


class RuleShape(Enum):
    """How the material editor renders this rule's inputs."""

    TARGET_AND_LIMIT = "target_and_limit"
    TARGET_ONLY = "target_only"
    LIMIT_ONLY = "limit_only"
    MIN_AND_MAX = "min_and_max"
    BINARY = "binary"


class RuleFamily(Enum):
    """Groups rules so the material editor stays navigable.

    Once machining lands there are far too many rules to present as one flat
    list, and a machinist looking for hole policy should not have to scroll
    past sheet-metal bend rules to find it.
    """

    GENERAL = "general"
    HOLE = "hole"
    THREAD = "thread"
    POCKET = "pocket"
    SLOT = "slot"
    THIN_FEATURE = "thin_feature"
    BOSS = "boss"
    RIB = "rib"
    BLEND = "blend"
    FREEFORM = "freeform"
    TOOL_ACCESS = "tool_access"
    SETUP = "setup"
    PART = "part"
    SHEET = "sheet"
    GDT = "gdt"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()

    @property
    def order(self) -> int:
        """Display position, so families appear in a sensible reading order."""
        return _FAMILY_ORDER.index(self)


_FAMILY_ORDER: list["RuleFamily"] = [
    RuleFamily.GENERAL,
    RuleFamily.PART,
    RuleFamily.SETUP,
    RuleFamily.TOOL_ACCESS,
    RuleFamily.HOLE,
    RuleFamily.THREAD,
    RuleFamily.POCKET,
    RuleFamily.SLOT,
    RuleFamily.THIN_FEATURE,
    RuleFamily.BOSS,
    RuleFamily.RIB,
    RuleFamily.BLEND,
    RuleFamily.FREEFORM,
    RuleFamily.SHEET,
    RuleFamily.GDT,
]


@dataclass(frozen=True)
class RuleType:
    """Static metadata for one rule.

    unit is the suffix shown inside the number input (e.g. mm, °).
    unit_suffix is a phrase shown under the input for ratio rules
    (e.g. "of wall thickness"), empty for absolute values.
    field_labels overrides the shape's default input labels.
    description is one short line shown under the rule name.
    family groups the rule in the material editor.
    """

    label: str
    shape: RuleShape
    unit: Optional[str] = "mm"
    comparison: str = "min"
    unit_suffix: str = ""
    description: str = ""
    field_labels: tuple[str, ...] = ()
    family: RuleFamily = RuleFamily.GENERAL


SHAPE_DEFAULT_LABELS: dict[RuleShape, tuple[str, ...]] = {
    RuleShape.TARGET_AND_LIMIT: ("Aim for", "At least"),
    RuleShape.TARGET_ONLY: ("Aim for",),
    RuleShape.LIMIT_ONLY: ("At most",),
    RuleShape.MIN_AND_MAX: ("Between", "and"),
    RuleShape.BINARY: ("If detected",),
}


class Rulebook(Enum):
    MIN_DRAFT_ANGLE = RuleType(
        "Minimum Draft Angle",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="°",
        comparison="min",
        description="Minimum inclination of faces relative to a reference axis.",
    )
    MIN_WALL_THICKNESS = RuleType(
        "Minimum Wall Thickness",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="mm",
        comparison="min",
        description="Minimum distance between opposing surfaces.",
    )
    MAX_WALL_THICKNESS = RuleType(
        "Maximum Wall Thickness",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="mm",
        comparison="max",
        field_labels=("Aim for", "At most"),
        description="Maximum distance between opposing surfaces.",
    )
    NO_UNDERCUTS = RuleType(
        "Undercut",
        shape=RuleShape.BINARY,
        unit=None,
        description="Geometry occluded by other features along a reference axis.",
    )
    SHARP_INTERNAL_CORNERS = RuleType(
        "Sharp Internal Corners",
        shape=RuleShape.BINARY,
        unit="°",
        description="Concave intersections of surfaces without a radius.",
    )
    SHARP_EXTERNAL_CORNERS = RuleType(
        "Sharp External Corners",
        shape=RuleShape.BINARY,
        unit="°",
        description="Convex intersections of surfaces without a radius.",
    )
    MAX_OVERHANG_ANGLE = RuleType(
        "Maximum Overhang Angle",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="°",
        comparison="max",
        field_labels=("Aim for", "At most"),
        description="Maximum unsupported surface angle relative to the print orientation.",
    )
    MAX_BRIDGE_SPAN = RuleType(
        "Maximum Bridge Span",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="mm",
        comparison="max",
        field_labels=("Aim for", "At most"),
        description="Maximum unsupported horizontal span between two supported regions.",
    )

    # -- machining: part ----------------------------------------------------
    PART_ASPECT_RATIO = RuleType(
        "Part Slenderness",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="",
        comparison="max",
        field_labels=("Aim under", "At most"),
        description="Ratio of the part's longest dimension to its shortest.",
        family=RuleFamily.PART,
    )
    MATERIAL_REMOVAL = RuleType(
        "Material Removal",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="%",
        comparison="max",
        field_labels=("Aim under", "At most"),
        description="Share of the stock volume cut away to reach the finished part.",
        family=RuleFamily.PART,
    )
    SEALED_VOID = RuleType(
        "Sealed Void",
        shape=RuleShape.BINARY,
        unit=None,
        description="Enclosed cavity with no tool access from outside the part.",
        family=RuleFamily.PART,
    )

    # -- machining: pockets and slots ---------------------------------------
    POCKET_DEPTH_RATIO = RuleType(
        "Pocket Depth Ratio",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="",
        comparison="max",
        field_labels=("Aim under", "At most"),
        description="Pocket depth as a multiple of its narrowest width.",
        family=RuleFamily.POCKET,
    )
    POCKET_CORNER_RADIUS = RuleType(
        "Pocket Corner Radius",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Inside corner radius, which a rotating cutter cannot make sharp.",
        family=RuleFamily.POCKET,
    )
    POCKET_NARROW_OPENING = RuleType(
        "Pocket Too Narrow",
        shape=RuleShape.BINARY,
        unit=None,
        description="Pocket narrower than the smallest cutter that can clear it.",
        family=RuleFamily.POCKET,
    )
    SLOT_DEPTH_RATIO = RuleType(
        "Slot Depth Ratio",
        shape=RuleShape.LIMIT_ONLY,
        unit="",
        comparison="max",
        field_labels=("At most",),
        description="Slot depth as a multiple of its width.",
        family=RuleFamily.SLOT,
    )
    SLOT_OVERHANG = RuleType(
        "Slot Cutter Overhang",
        shape=RuleShape.BINARY,
        unit=None,
        description="Long deep slot where the cutter runs at full stickout.",
        family=RuleFamily.SLOT,
    )

    # -- machining: thin features -------------------------------------------
    THIN_WALL = RuleType(
        "Thin Wall",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="mm",
        comparison="min",
        description="Minimum material left between two opposing surfaces.",
        family=RuleFamily.THIN_FEATURE,
    )

    # -- machining: holes ---------------------------------------------------
    HOLE_DEPTH_RATIO = RuleType(
        "Hole Depth Ratio",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="",
        comparison="max",
        field_labels=("Aim under", "At most"),
        description="Hole depth as a multiple of its diameter.",
        family=RuleFamily.HOLE,
    )
    HOLE_EDGE_DISTANCE = RuleType(
        "Hole Edge Distance",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Material left between a hole and the outside of the part.",
        family=RuleFamily.HOLE,
    )
    HOLE_WEB_THICKNESS = RuleType(
        "Hole Web Thickness",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Material left between two parallel holes.",
        family=RuleFamily.HOLE,
    )
    HOLE_FLAT_BOTTOM = RuleType(
        "Flat-Bottomed Hole",
        shape=RuleShape.BINARY,
        unit=None,
        description="Blind hole with a flat floor, which a twist drill cannot produce.",
        family=RuleFamily.HOLE,
    )
    HOLE_INTERSECTING = RuleType(
        "Intersecting Holes",
        shape=RuleShape.BINARY,
        unit=None,
        description="Holes that break into one another, leaving an interrupted cut.",
        family=RuleFamily.HOLE,
    )

    # -- machining: setup and workholding -----------------------------------
    NO_DATUM_FACE = RuleType(
        "Datum Face",
        shape=RuleShape.BINARY,
        unit=None,
        description="No flat face large enough to locate the part against.",
        family=RuleFamily.SETUP,
    )
    NO_PARALLEL_DATUM_PAIR = RuleType(
        "Parallel Clamping Faces",
        shape=RuleShape.BINARY,
        unit=None,
        description="No opposed pair of flat faces for a vise to grip.",
        family=RuleFamily.SETUP,
    )
    THIN_CLAMPING_DIMENSION = RuleType(
        "Clamping Thickness",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Minimum stock thickness available for a vise to clamp on.",
        family=RuleFamily.SETUP,
    )
    SMALL_PART_HOLDING = RuleType(
        "Small Part Holding",
        shape=RuleShape.BINARY,
        unit=None,
        description="Part too small for vise work; needs soft jaws, a fixture or a pallet.",
        family=RuleFamily.SETUP,
    )

    @property
    def id(self) -> str:
        return self.name

    @property
    def label(self) -> str:
        return self.value.label

    @property
    def unit(self) -> str:
        return self.value.unit or ""

    @property
    def unit_suffix(self) -> str:
        return self.value.unit_suffix

    @property
    def description(self) -> str:
        return self.value.description

    @property
    def shape(self) -> RuleShape:
        return self.value.shape

    @property
    def family(self) -> RuleFamily:
        return self.value.family

    @property
    def is_binary(self) -> bool:
        return self.value.shape == RuleShape.BINARY

    @property
    def comparison(self) -> str:
        return self.value.comparison

    @property
    def field_labels(self) -> tuple[str, ...]:
        if self.value.field_labels:
            return self.value.field_labels
        return SHAPE_DEFAULT_LABELS[self.shape]
