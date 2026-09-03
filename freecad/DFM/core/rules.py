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
    GROOVE = "groove"
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
    RuleFamily.GROOVE,
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

    # -- machining: grooves -------------------------------------------------
    THREAD_RELIEF_WIDTH = RuleType(
        "Thread Relief Width",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="mm",
        comparison="min",
        description=(
            "Width of a relief groove at the end of a thread, so the tool "
            "has somewhere to run out."
        ),
        family=RuleFamily.GROOVE,
    )
    GROOVE_SQUARE_CORNER = RuleType(
        "Groove Square Corner",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description=(
            "Corner radius of a milled gasket-groove loop. A rotating cutter "
            "cannot produce a square corner."
        ),
        family=RuleFamily.GROOVE,
    )

    # -- machining: blends --------------------------------------------------
    CUTTER_RADIUS_INFEASIBLE = RuleType(
        "Cutter Radius Infeasible",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description=(
            "Internal corner radius smaller than any tool in the library can "
            "produce."
        ),
        family=RuleFamily.BLEND,
    )
    CUTTER_RADIUS_SUBOPTIMAL = RuleType(
        "Cutter Radius Suboptimal",
        shape=RuleShape.BINARY,
        unit=None,
        description=(
            "Internal corner radius that needs a specialty cutter rather than "
            "a standard end mill. Achievable, but not off the shelf."
        ),
        family=RuleFamily.BLEND,
    )
    CHAMFER_NONSTANDARD_ANGLE = RuleType(
        "Chamfer Nonstandard Angle",
        shape=RuleShape.BINARY,
        unit=None,
        description="Chamfer cut at an angle other than 45 degrees.",
        family=RuleFamily.BLEND,
    )
    METAL_SEAL_WITNESS = RuleType(
        "Metal Seal Witness Edge",
        shape=RuleShape.BINARY,
        unit=None,
        description=(
            "Functionally sharp revolved sealing edge, as on a knife-edge "
            "flange."
        ),
        family=RuleFamily.BLEND,
    )

    # -- machining: bosses --------------------------------------------------
    BOSS_HEIGHT_RATIO = RuleType(
        "Boss Height Ratio",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="",
        comparison="max",
        description="Boss height as a multiple of its diameter or least side.",
        family=RuleFamily.BOSS,
    )
    BOSS_WALL_THICKNESS = RuleType(
        "Boss Wall Thickness",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="mm",
        comparison="min",
        description="Boss diameter, below which its wall is too thin to machine.",
        family=RuleFamily.BOSS,
    )
    BOSS_UNDERCUT = RuleType(
        "Boss Needs Special Fixturing",
        shape=RuleShape.BINARY,
        unit=None,
        description="Boss standing off an axis the machine cannot reach directly.",
        family=RuleFamily.BOSS,
    )

    # -- machining: ribs ----------------------------------------------------
    RIB_HEIGHT_ASPECT = RuleType(
        "Rib Height Aspect",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="",
        comparison="max",
        description="Rib height as a multiple of its thickness.",
        family=RuleFamily.RIB,
    )
    RIB_DRAFT_ANGLE = RuleType(
        "Rib Draft Angle",
        shape=RuleShape.LIMIT_ONLY,
        unit="deg",
        comparison="min",
        field_labels=("At least",),
        description="Draft on a rib wall, for casting and for tool release.",
        family=RuleFamily.RIB,
    )

    # -- machining: sculpted surfaces ---------------------------------------
    FREEFORM_INTERNAL_RADIUS = RuleType(
        "Freeform Internal Radius",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="mm",
        comparison="min",
        description=(
            "Tightest concave radius in a sculpted region, against the "
            "smallest ball nose available."
        ),
        family=RuleFamily.FREEFORM,
    )
    FREEFORM_FINISHING = RuleType(
        "Freeform Finishing Burden",
        shape=RuleShape.BINARY,
        unit=None,
        description="Sculpted area needing a long ball-nose finishing pass.",
        family=RuleFamily.FREEFORM,
    )
    TURNED_PROFILE_RADIUS = RuleType(
        "Turned Profile Radius",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Concave valley in a turned profile, against the form tool nose.",
        family=RuleFamily.FREEFORM,
    )

    # -- machining: spherical pockets ---------------------------------------
    SPHERICAL_POCKET_UNDERCUT = RuleType(
        "Spherical Pocket Undercut",
        shape=RuleShape.BINARY,
        unit=None,
        description="Ball-ended pocket whose opening is narrower than its equator.",
        family=RuleFamily.TOOL_ACCESS,
    )

    # -- machining: threads -------------------------------------------------
    THREAD_RUNOUT = RuleType(
        "Thread Runout",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Clearance below a tapped blind hole for the tap to run out.",
        family=RuleFamily.THREAD,
    )
    THREAD_SHOULDER_PROXIMITY = RuleType(
        "Thread Shoulder Proximity",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Room between a thread and the shoulder it runs into.",
        family=RuleFamily.THREAD,
    )
    THREAD_WALL_THICKNESS = RuleType(
        "Thread Wall Thickness",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="mm",
        comparison="min",
        description="Material left around a tapped hole.",
        family=RuleFamily.THREAD,
    )

    # -- machining: more hole policy ----------------------------------------
    HOLE_NONSTANDARD_DIAMETER = RuleType(
        "Hole Nonstandard Diameter",
        shape=RuleShape.BINARY,
        unit=None,
        description="Bore at a diameter no stock drill in the library produces.",
        family=RuleFamily.HOLE,
    )
    HOLE_PARTIAL_ENTRY = RuleType(
        "Hole Partial Entry",
        shape=RuleShape.BINARY,
        unit=None,
        description="Drill entering on a broken or sloped face, so it will wander.",
        family=RuleFamily.HOLE,
    )
    HOLE_COUNTERSINK_ANGLE = RuleType(
        "Countersink Angle",
        shape=RuleShape.BINARY,
        unit=None,
        description="Countersink cut at an angle no standard tool produces.",
        family=RuleFamily.HOLE,
    )
    HOLE_MULTI_PASS = RuleType(
        "Hole Multi Pass",
        shape=RuleShape.BINARY,
        unit=None,
        description="Bore too deep for one plunge, needing peck or a second tool.",
        family=RuleFamily.HOLE,
    )
    HOLE_INTERSECTS_CAVITY = RuleType(
        "Hole Intersects Cavity",
        shape=RuleShape.BINARY,
        unit=None,
        description="Drill breaking into a pocket or slot part way down.",
        family=RuleFamily.HOLE,
    )

    # -- machining: more cavity policy --------------------------------------
    POCKET_ASPECT_RATIO = RuleType(
        "Pocket Reach",
        shape=RuleShape.BINARY,
        unit=None,
        description=(
            "Pocket deeper than the flute length of the longest cutter that "
            "will fit in it."
        ),
        family=RuleFamily.POCKET,
    )
    SLOT_NONSTANDARD_WIDTH = RuleType(
        "Slot Nonstandard Width",
        shape=RuleShape.BINARY,
        unit=None,
        description="Slot at a width no cutter in the library matches.",
        family=RuleFamily.SLOT,
    )
    FLEXURE_SLIT_PROCESS = RuleType(
        "Flexure Slit Process",
        shape=RuleShape.BINARY,
        unit=None,
        description="Slit too narrow to mill, needing wire EDM or a saw.",
        family=RuleFamily.SLOT,
    )
    BROACHED_SLOT_PROCESS = RuleType(
        "Broached Slot Process",
        shape=RuleShape.BINARY,
        unit=None,
        description="Square-cornered internal slot that has to be broached.",
        family=RuleFamily.SLOT,
    )

    # -- machining: more part policy ----------------------------------------
    MINIMUM_FEATURE_SIZE = RuleType(
        "Minimum Feature Size",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Smallest detail the shop's smallest tool can actually cut.",
        family=RuleFamily.PART,
    )
    SHARP_INTERNAL_EDGE = RuleType(
        "Sharp Internal Edge",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Square inside corner a rotating cutter cannot produce.",
        family=RuleFamily.PART,
    )
    PART_MARKING = RuleType(
        "Part Marking",
        shape=RuleShape.BINARY,
        unit=None,
        description="Engraved or embossed text, and how it should be applied.",
        family=RuleFamily.PART,
    )
    RAISED_TEXT_MACHINED_FACE = RuleType(
        "Raised Text on a Machined Face",
        shape=RuleShape.BINARY,
        unit=None,
        description="Text left standing proud, so the field around it must be cleared.",
        family=RuleFamily.PART,
    )
    FEATURE_COMPLEXITY = RuleType(
        "Feature Complexity",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="",
        comparison="max",
        description="How many distinct features the part carries, as a cost signal.",
        family=RuleFamily.PART,
    )
    CASTING_DRAFT_ANGLE = RuleType(
        "Casting Draft Angle",
        shape=RuleShape.LIMIT_ONLY,
        unit="deg",
        comparison="min",
        field_labels=("At least",),
        description="Draft on a wall, for a part meant to come out of a mould.",
        family=RuleFamily.PART,
    )

    # -- machining: more setup policy ---------------------------------------
    SETUP_COUNT_HIGH = RuleType(
        "Setup Count High",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="",
        comparison="max",
        description="How many times the part must be refixtured.",
        family=RuleFamily.SETUP,
    )
    NO_ORTHOGONAL_DATUM_TRIO = RuleType(
        "No Orthogonal Datum Trio",
        shape=RuleShape.BINARY,
        unit=None,
        description="No three square faces to locate the part from.",
        family=RuleFamily.SETUP,
    )
    TOOL_ACCESS_SPECIAL_SETUP = RuleType(
        "Tool Access Special Setup",
        shape=RuleShape.BINARY,
        unit=None,
        description="Feature reachable only from an angle needing a fixture or a fourth axis.",
        family=RuleFamily.TOOL_ACCESS,
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

    # -- machining: tool access ---------------------------------------------
    UNDERCUT_PRESENT = RuleType(
        "Undercut",
        shape=RuleShape.BINARY,
        unit=None,
        description="Surface no straight-down approach can reach.",
        family=RuleFamily.TOOL_ACCESS,
    )
    TOOL_ACCESS_BLOCKED = RuleType(
        "Unreachable Feature",
        shape=RuleShape.BINARY,
        unit=None,
        description="Feature with no tool approach at all, from any direction.",
        family=RuleFamily.TOOL_ACCESS,
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


    # -- inspection: geometric dimensioning and tolerancing -----------------
    #
    # Dormant. These read tolerance and surface-finish annotations, which a
    # FreeCAD document does not carry today -- the workbench has nowhere to
    # get a feature control frame from. They are ported and registered so
    # that the day the annotations arrive the policy is already written and
    # already editable by the shop, rather than being a second project.
    GDT_TOLERANCE_ACHIEVABLE = RuleType(
        "Tolerance Achievable",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Tolerance tighter than the process can hold.",
        family=RuleFamily.GDT,
    )
    GDT_DATUM_VALID = RuleType(
        "Datum Valid",
        shape=RuleShape.BINARY,
        unit=None,
        description="Datum feature too small or too rough to locate from.",
        family=RuleFamily.GDT,
    )
    GDT_DATUM_UNRESOLVED = RuleType(
        "Datum Unresolved",
        shape=RuleShape.BINARY,
        unit=None,
        description="Control frame referencing a datum the model does not define.",
        family=RuleFamily.GDT,
    )
    GDT_SURFACE_FINISH_CONFLICT = RuleType(
        "Surface Finish Conflict",
        shape=RuleShape.BINARY,
        unit=None,
        description="Finish called out that the specified process cannot deliver.",
        family=RuleFamily.GDT,
    )
    GDT_FEATURE_TOLERANCE_MISMATCH = RuleType(
        "Feature Tolerance Mismatch",
        shape=RuleShape.BINARY,
        unit=None,
        description="Tolerance inappropriate for the kind of feature it is on.",
        family=RuleFamily.GDT,
    )
    NOTE_SURFACE_FINISH_DEMANDING = RuleType(
        "Demanding Surface Finish Note",
        shape=RuleShape.BINARY,
        unit=None,
        description="A general finish note that will drive a separate operation.",
        family=RuleFamily.GDT,
    )
    SURFACE_FINISH_PER_FACE_DEMANDING = RuleType(
        "Demanding Surface Finish on a Face",
        shape=RuleShape.BINARY,
        unit=None,
        description="A face-level finish callout needing grinding or lapping.",
        family=RuleFamily.GDT,
    )


    # -- sheet metal --------------------------------------------------------
    #
    # A formed part is judged against the press and the brake rather than
    # against a cutter, so almost nothing in the machining families applies
    # to it. These stand in their place: the die has a minimum bend radius,
    # the brake needs a flange long enough to grip, a punch breaks below a
    # certain hole size, and material stretches only so far before it tears.
    SHEET_BEND_RADIUS_SMALL = RuleType(
        "Bend Radius Too Small",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="",
        comparison="min",
        description="Inside bend radius as a multiple of the material thickness.",
        family=RuleFamily.SHEET,
    )
    SHEET_FLANGE_SHORT = RuleType(
        "Flange Too Short",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="mm",
        comparison="min",
        description="Flange length past the bend, for the brake die to grip.",
        family=RuleFamily.SHEET,
    )
    SHEET_HOLE_NEAR_BEND = RuleType(
        "Hole Near a Bend",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Distance from a hole to the bend that would distort it.",
        family=RuleFamily.SHEET,
    )
    SHEET_HOLE_SMALL = RuleType(
        "Hole Too Small to Punch",
        shape=RuleShape.LIMIT_ONLY,
        unit="",
        comparison="min",
        field_labels=("At least",),
        description="Hole diameter as a multiple of thickness, before the punch breaks.",
        family=RuleFamily.SHEET,
    )
    SHEET_HOLE_PITCH = RuleType(
        "Hole Pitch Too Close",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Web left between adjacent punched holes.",
        family=RuleFamily.SHEET,
    )
    SHEET_COUNTERSINK_DEEP = RuleType(
        "Countersink Too Deep for the Gauge",
        shape=RuleShape.BINARY,
        unit=None,
        description="Countersink leaving a knife edge on the far side of the sheet.",
        family=RuleFamily.SHEET,
    )
    SHEET_CLOSED_FLANGE_LOOP = RuleType(
        "Closed Flange Loop",
        shape=RuleShape.BINARY,
        unit=None,
        description="A profile that cannot be folded from one flat blank without a seam.",
        family=RuleFamily.SHEET,
    )
    SHEET_BEND_ANGLE_EXTREME = RuleType(
        "Extreme Bend Angle",
        shape=RuleShape.LIMIT_ONLY,
        unit="deg",
        comparison="max",
        field_labels=("At most",),
        description="Bend past the point where springback and tooling get awkward.",
        family=RuleFamily.SHEET,
    )
    SHEET_BEND_LONGER_THAN_BODY = RuleType(
        "Bend Longer Than the Brake",
        shape=RuleShape.BINARY,
        unit=None,
        description="Bend line longer than the press brake can take in one hit.",
        family=RuleFamily.SHEET,
    )
    SHEET_THICKNESS_OUT_OF_RANGE = RuleType(
        "Gauge Out of Range",
        shape=RuleShape.MIN_AND_MAX,
        unit="mm",
        description="Material thickness against the gauges the shop stocks and forms.",
        family=RuleFamily.SHEET,
    )
    SHEET_BEND_RELIEF_MISSING = RuleType(
        "Bend Relief Missing",
        shape=RuleShape.BINARY,
        unit=None,
        description="A bend ending mid-panel with no relief cut, so the sheet tears.",
        family=RuleFamily.SHEET,
    )
    SHEET_CORNER_RELIEF_MISSING = RuleType(
        "Corner Relief Missing",
        shape=RuleShape.BINARY,
        unit=None,
        description="Two bends meeting at a corner whose flanges will collide.",
        family=RuleFamily.SHEET,
    )
    SHEET_TAB_NARROW = RuleType(
        "Tab Too Narrow",
        shape=RuleShape.LIMIT_ONLY,
        unit="",
        comparison="min",
        field_labels=("At least",),
        description="Tab width as a multiple of thickness, before it bends in handling.",
        family=RuleFamily.SHEET,
    )
    SHEET_NOTCH_NARROW = RuleType(
        "Notch Too Narrow",
        shape=RuleShape.LIMIT_ONLY,
        unit="",
        comparison="min",
        field_labels=("At least",),
        description="Notch width as a multiple of thickness, before the punch will not clear.",
        family=RuleFamily.SHEET,
    )
    SHEET_HEM_DIMENSIONS = RuleType(
        "Hem Dimensions",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Hem return length, and whether the gauge will close flat at all.",
        family=RuleFamily.SHEET,
    )
    SHEET_FORMED_FEATURE = RuleType(
        "Formed Feature",
        shape=RuleShape.BINARY,
        unit=None,
        description="An emboss, louver or lance, each needing a die of its own.",
        family=RuleFamily.SHEET,
    )
    SHEET_EMBOSS_DEEP = RuleType(
        "Emboss Too Deep",
        shape=RuleShape.LIMIT_ONLY,
        unit="",
        comparison="max",
        field_labels=("At most",),
        description="Draw depth as a multiple of thickness, before the material tears.",
        family=RuleFamily.SHEET,
    )
    SHEET_LOUVER_TALL = RuleType(
        "Louver Too Tall",
        shape=RuleShape.LIMIT_ONLY,
        unit="",
        comparison="max",
        field_labels=("At most",),
        description="Louver hood height against the range standard dies cover.",
        family=RuleFamily.SHEET,
    )
    SHEET_FORMED_PITCH = RuleType(
        "Formed Features Too Close",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="Spacing between formed features, so the dies do not collide.",
        family=RuleFamily.SHEET,
    )
    SHEET_SHARP_FOLD = RuleType(
        "Sharp Fold",
        shape=RuleShape.BINARY,
        unit=None,
        description="A fold modelled with no radius, which no brake produces.",
        family=RuleFamily.SHEET,
    )
    SHEET_FORMED_NEAR_BEND = RuleType(
        "Formed Feature Near a Bend",
        shape=RuleShape.LIMIT_ONLY,
        unit="mm",
        comparison="min",
        field_labels=("At least",),
        description="A drawn feature inside the zone a bend deforms.",
        family=RuleFamily.SHEET,
    )
    SHEET_FEATURE_COMPLEXITY = RuleType(
        "Sheet Feature Complexity",
        shape=RuleShape.TARGET_AND_LIMIT,
        unit="",
        comparison="max",
        description="How many bends and formed features the part carries, as a cost signal.",
        family=RuleFamily.SHEET,
    )
    SHEET_INTENT_SHARP_CORNERS = RuleType(
        "Sheet Intent, Sharp Corners",
        shape=RuleShape.BINARY,
        unit=None,
        description=(
            "A part shaped like sheet metal but modelled with sharp folds and "
            "machined features."
        ),
        family=RuleFamily.SHEET,
    )
    SHEET_MACHINED_FEATURE = RuleType(
        "Machined Feature on a Formed Part",
        shape=RuleShape.BINARY,
        unit=None,
        description="A secondary machining operation on a part that is otherwise formed.",
        family=RuleFamily.SHEET,
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
