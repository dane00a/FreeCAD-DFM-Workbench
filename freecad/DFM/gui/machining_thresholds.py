# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The Limits page: every number a machining rule compares against.

There are a hundred-odd of them, and a column of a hundred-odd spinboxes is
not an editor. So they are dealt out by the concern they belong to -- holes,
threads, pockets, sheet forming -- one page per concern, in the order a shop
meets them. The dozen a shop actually revisits are set in bold; the rest are
set once when the workbench is first pointed at the shop, and then left.

Nothing here decides anything. The tables map a `RuleThresholds` field to a
label, a unit and a workable range, and `threshold_panels()` turns that into
widgets. Keeping the two apart is deliberate: the tables have to be readable
without Qt on the machine, because whether every field is reachable is a
question about the tables and not about the dialog.

Two things are derived rather than declared, because declaring them is how
they go wrong:

* The default comes from `RuleThresholds` itself, so the page can never show
  a number the rules do not actually start from.
* Whether a field is a whole number is decided the same way
  `RuleThresholds.apply_overrides` decides it -- by the type of its own
  default. That is what lines the spinbox choice up with the typed parameter
  setter, and it is what keeps a float threshold from being written with
  `SetInt` and quietly losing its decimals.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from typing import Optional

from ..core.machining.config import THRESHOLD_PREF_PREFIX, RuleThresholds


# =============================================================================
# Units
# =============================================================================
#
# A unit fixes the suffix and a workable range and step, so a spec only has to
# name it. Overriding a range on a single spec is for the odd field whose unit
# is right but whose scale is not.

MM = "mm"
TOL_MM = "tol_mm"
MM2 = "mm2"
DEG = "deg"
UM = "um"
RATIO = "ratio"
GAUGE = "gauge"
PITCH = "pitch"
PCT = "pct"
DOT = "dot"
SHARE = "share"
NUMBER = "number"
COUNT = "count"


@dataclass(frozen=True)
class _UnitStyle:
    suffix: str
    minimum: float
    maximum: float
    step: float
    decimals: int


_UNIT_STYLES = {
    MM: _UnitStyle(" mm", 0.0, 1000.0, 0.1, 3),
    # A ground or lapped limit is single microns, so it needs a step that can
    # walk there without holding the arrow down.
    TOL_MM: _UnitStyle(" mm", 0.0, 10.0, 0.005, 4),
    MM2: _UnitStyle(" mm²", 0.0, 100000.0, 10.0, 1),
    DEG: _UnitStyle("°", 0.0, 360.0, 0.5, 2),
    UM: _UnitStyle(" µm", 0.0, 1000.0, 0.1, 3),
    RATIO: _UnitStyle("×", 0.0, 100.0, 0.1, 2),
    GAUGE: _UnitStyle("× gauge", 0.0, 50.0, 0.1, 2),
    PITCH: _UnitStyle(" pitches", 0.0, 50.0, 0.5, 2),
    PCT: _UnitStyle(" %", 0.0, 100.0, 1.0, 1),
    # A cosine. Negative is meaningful: it is how two faces are told to be
    # back to back rather than merely off square.
    DOT: _UnitStyle("", -1.0, 1.0, 0.01, 3),
    SHARE: _UnitStyle("", 0.0, 1.0, 0.01, 3),
    NUMBER: _UnitStyle("", 0.0, 10000.0, 1.0, 0),
    COUNT: _UnitStyle("", 0.0, 999.0, 1.0, 0),
}


# =============================================================================
# Specs
# =============================================================================


@dataclass(frozen=True)
class ThresholdSpec:
    """One `RuleThresholds` field as the page presents it."""

    field: str
    label: str
    unit: str = MM
    # Set on the handful a shop revisits as its work changes, rather than the
    # long tail it sets once. Shown in bold.
    common: bool = False
    # Only where the label genuinely cannot carry the reason.
    note: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    decimals: Optional[int] = None

    @property
    def style(self) -> _UnitStyle:
        base = _UNIT_STYLES[self.unit]
        return _UnitStyle(
            suffix=base.suffix,
            minimum=base.minimum if self.minimum is None else self.minimum,
            maximum=base.maximum if self.maximum is None else self.maximum,
            step=base.step if self.step is None else self.step,
            decimals=base.decimals if self.decimals is None else self.decimals,
        )


@dataclass(frozen=True)
class ThresholdGroup:
    """One page of the editor.

    `blurb` carries whatever the group as a whole needs said, so the labels
    underneath do not each repeat it.
    """

    title: str
    blurb: str
    specs: tuple[ThresholdSpec, ...]


def _spec(field: str, label: str, unit: str = MM, **kwargs) -> ThresholdSpec:
    return ThresholdSpec(field=field, label=label, unit=unit, **kwargs)


THRESHOLD_GROUPS: tuple[ThresholdGroup, ...] = (
    ThresholdGroup(
        "Holes and bores",
        "Depth is expressed against diameter throughout, because that is "
        "what a drill feels. A bore cut with a boring bar gets its own pair "
        "of ratios: a bar overhangs its holder and deflects long before a "
        "drill of the same reach would.",
        (
            _spec("hole_deep_warn_ratio", "Deep hole, warn above", RATIO, common=True),
            _spec("hole_deep_error_ratio", "Deep hole, fault above", RATIO, common=True),
            _spec("hole_deep_bore_warn_ratio", "Bored on-axis, warn above", RATIO),
            _spec("hole_deep_bore_error_ratio", "Bored on-axis, fault above", RATIO),
            _spec(
                "hole_deep_single_pass_void_ratio",
                "Crossing void that splits the drilled run",
                RATIO,
                note="A void wider than this many diameters means the hole "
                "is drilled from both sides, so only the longest unbroken "
                "run counts as depth.",
            ),
            _spec("hole_edge_distance_mm", "Hole to part edge, minimum"),
            _spec("hole_web_thickness_mm", "Web left between holes, minimum"),
            _spec("hole_flat_bottom_max_diameter_mm", "Flat-bottom hole, largest cut"),
            _spec("hole_flat_bottom_min_diameter_mm", "Flat-bottom hole, smallest cut"),
            _spec(
                "hole_nonstandard_max_diameter_mm",
                "Check against the drill index up to",
                note="Above this a hole is bored to size, so the drill "
                "index has nothing to say about it.",
            ),
            _spec(
                "hole_intersecting_network_threshold",
                "Intersecting holes before the network is called out",
                COUNT,
            ),
            _spec(
                "hole_intersecting_thin_plate_dd_max",
                "Intersecting holes in thin plate, depth over diameter",
                RATIO,
            ),
            _spec("standard_size_match_tol_mm", "Match a stocked size within", TOL_MM),
            _spec(
                "hole_partial_entry_min_depth_ratio",
                "Skewed entry matters past this slenderness",
                RATIO,
            ),
            _spec(
                "hole_partial_entry_perpendicular_dot",
                "Entry face counts as square above",
                DOT,
            ),
            _spec("hole_countersink_angle_tol_deg", "Countersink angle match within", DEG),
        ),
    ),
    ThresholdGroup(
        "Threads",
        "A tap cannot cut to the bottom of a hole and a single-point tool "
        "cannot stop dead at a shoulder, so most of these are about the room "
        "the thread needs at either end of its run.",
        (
            _spec("thread_runout_min_pitches", "Runout at the end of a thread", PITCH),
            _spec(
                "thread_runout_min_diameters",
                "Runout when the pitch is unknown",
                RATIO,
                note="Falls back to diameters when nothing on the model "
                "says what pitch was intended.",
            ),
            _spec("thread_shoulder_min_mm", "Clearance to a shoulder, minimum"),
            _spec("thread_shoulder_min_height_mm", "Shoulder height before it obstructs"),
            _spec("thread_relief_min_width_mm", "Relief groove width, minimum"),
            _spec("thread_relief_pitch_multiplier", "Relief groove width in pitches", RATIO),
            _spec("thread_diameter_mismatch_mm", "Nominal against modelled diameter, allow"),
            _spec("thread_cut_depth_ratio", "Thread root depth as a share of nominal", SHARE),
        ),
    ),
    ThresholdGroup(
        "Pockets",
        "Depth is against the pocket's narrowest width, since that is what "
        "sets the largest cutter that will fit, and so the stickout it has "
        "to reach with.",
        (
            _spec("pocket_deep_warn_ratio", "Deep pocket, warn above", RATIO, common=True),
            _spec("pocket_deep_error_ratio", "Deep pocket, fault above", RATIO, common=True),
            _spec(
                "pocket_corner_radius_min_mm",
                "Report an inside corner at or below",
                note="Zero reports every sharp corner, which is usually "
                "what is wanted: nothing mills a sharp inside corner.",
            ),
            _spec(
                "pocket_narrow_opening_tool_multiple",
                "Pocket width in cutter diameters, minimum",
                RATIO,
            ),
        ),
    ),
    ThresholdGroup(
        "Slots and channels",
        "A long slot is cut with a cutter that is by definition thinner than "
        "the slot, so length and depth both work against it. Chatter needs "
        "both, which is why the overhang check is gated on depth as well.",
        (
            _spec("slot_deep_warn_ratio", "Deep slot, warn above", RATIO),
            _spec("slot_overhang_warn_ratio", "Slot length over width, warn above", RATIO),
            _spec(
                "slot_overhang_depth_gate_ratio",
                "...and only with this much stickout",
                RATIO,
            ),
            _spec("slot_nonstandard_width_max_mm", "Check against stocked cutters up to"),
        ),
    ),
    ThresholdGroup(
        "Thin walls",
        "Deflection goes with thickness cubed, so a wall that is thick "
        "enough is rigid at any practical height. The thickness cap is what "
        "keeps the height check off sections no machinist would call a wall.",
        (
            _spec("thin_wall_warn_mm", "Thin wall, warn below", common=True),
            _spec("thin_wall_error_mm", "Thin wall, fault below", common=True),
            _spec("thin_wall_aspect_warn", "Wall height over thickness, warn above", RATIO),
            _spec(
                "thin_wall_aspect_max_thickness_mm",
                "Skip the height check above this thickness",
            ),
        ),
    ),
    ThresholdGroup(
        "Bosses, ribs and domes",
        "Anything standing proud is held only at its root, so it is measured "
        "by how far it stands against how wide it stands.",
        (
            _spec("rib_height_aspect_warn", "Rib height over thickness, warn above", RATIO),
            _spec("rib_min_draft_angle_deg", "Draft on a rib, minimum", DEG),
            _spec("boss_min_diameter_mm", "Boss diameter, minimum"),
            _spec("boss_height_warn_ratio", "Boss height over base, warn above", RATIO),
            _spec("boss_height_error_ratio", "Boss height over base, fault above", RATIO),
            _spec(
                "boss_cardinal_alignment_min_dot",
                "Boss axis counts as machine-aligned above",
                DOT,
                note="An axis further off every machine cardinal than this "
                "needs its own indexed setup.",
            ),
            _spec(
                "spherical_overhang_warn_mm",
                "Dome overhanging its own opening, warn above",
            ),
        ),
    ),
    ThresholdGroup(
        "Setup and workholding",
        "How many times the part is turned over, and whether there is "
        "anything to grip while it is. These assume a vise unless the part "
        "is too small to be held in one, at which point the vise rules stand "
        "down and one holding note is raised instead.",
        (
            _spec(
                "tad_angular_cluster_deg",
                "Merge tool directions within",
                DEG,
                note="Directions closer together than this are one setup, "
                "not two.",
            ),
            _spec("setup_count_info_min", "Mention the setup count from", COUNT),
            _spec("setup_count_warn", "Setups, warn from", COUNT, common=True),
            _spec("setup_count_error", "Setups, fault from", COUNT),
            _spec("datum_face_min_area_mm2", "Datum face area, minimum", MM2),
            _spec("min_jaw_separation_mm", "Jaw separation, minimum"),
            _spec("min_clamping_thickness_mm", "Thickness to clamp on, minimum"),
            _spec("small_part_max_dim_mm", "Too small to vise below"),
            _spec("datum_perpendicular_max_dot", "Two datum faces count as square below", DOT),
            _spec(
                "shaped_cutter_angle_tol_deg",
                "Match a catalogue form cutter within",
                DEG,
                note="A form cutter is ground to an exact angle, so a wall "
                "tilt only matches one this closely.",
            ),
        ),
    ),
    ThresholdGroup(
        "Part envelope and cost",
        "The shape of the blank rather than of any one feature. None of "
        "these is a defect on its own -- a long bar is perfectly makeable, "
        "it just chatters -- but they are what programming and setup time "
        "actually follow.",
        (
            _spec("part_aspect_warn_ratio", "Longest over shortest, warn above", RATIO),
            _spec(
                "plate_mid_min_ratio",
                "Middle over shortest that reads as plate",
                RATIO,
                note="Once the aspect ratio trips, this is what separates a "
                "plate from a bar. Bars deflect; plates warp.",
            ),
            _spec("turn_slender_warn_ratio", "Turned length over OD, warn above", RATIO),
            _spec("turn_slender_error_ratio", "Turned length over OD, fault above", RATIO),
            _spec("minimum_feature_size_mm", "Smallest feature worth cutting", common=True),
            _spec("feature_complexity_warn", "Feature count, warn from", NUMBER),
            _spec("feature_complexity_error", "Feature count, fault from", NUMBER),
            _spec("material_removal_warn_pct", "Stock removed, warn above", PCT, common=True),
            _spec("material_removal_error_pct", "Stock removed, fault above", PCT),
        ),
    ),
    ThresholdGroup(
        "Blends and chamfers",
        "Whether an edge treatment is one a catalogue tool already cuts, or "
        "one that has to be programmed.",
        (
            _spec("sharp_edge_min_deviation_deg", "Edge counts as sharp past", DEG),
            _spec("chamfer_standard_angle_deg", "Standard chamfer angle", DEG),
            _spec("chamfer_angle_tol_deg", "Match the standard angle within", DEG),
        ),
    ),
    ThresholdGroup(
        "Freeform surfaces",
        "A ball nose leaves scallops between passes and cannot get into a "
        "concave radius tighter than its own. Both are about the finishing "
        "tool rather than about the surface.",
        (
            _spec(
                "freeform_radius_safety",
                "Concave radius must beat the tool radius by",
                RATIO,
            ),
            _spec("freeform_radius_info_tier_mm", "Note a concave radius below"),
            _spec("freeform_scallop_target_um", "Scallop height target", UM),
            _spec("freeform_blend_band_max_radius_mm", "Largest radius still read as a blend"),
            _spec("freeform_finishing_min_area_mm2", "Area worth a finishing pass", MM2),
        ),
    ),
    ThresholdGroup(
        "Sheet metal -- forming",
        "Almost everything here is a multiple of the gauge, because that is "
        "how a sheet shop thinks: a bend radius is one material thickness, "
        "not 1.5 mm. The absolute ones are absolute for a physical reason -- "
        "a brake has a gauge range whatever the part is.",
        (
            _spec(
                "sheet_bend_radius_warn_factor",
                "Bend radius, warn below",
                GAUGE,
                common=True,
            ),
            _spec("sheet_bend_radius_error_factor", "Bend radius, fault below", GAUGE),
            _spec("sheet_min_flange_factor", "Flange length, minimum", GAUGE),
            _spec("sheet_hem_min_return_factor", "Hem return length, minimum", GAUGE),
            _spec(
                "sheet_max_bend_deg_at_ga11",
                "Heaviest stock: over-bend up to",
                DEG,
                note="How far the brake can go past square before "
                "springback and tooling get awkward. Heavier stock takes "
                "less.",
            ),
            _spec("sheet_max_bend_deg_at_ga14", "Lightest stock: over-bend up to", DEG),
            _spec("sheet_min_thickness_mm", "Thinnest stock handled"),
            _spec("sheet_max_thickness_steel_mm", "Heaviest steel handled"),
            _spec("sheet_max_thickness_alu_mm", "Heaviest aluminium handled"),
            _spec(
                "sheet_diagonal_bend_min_thickness_mm",
                "Diagonal bend only matters from",
                note="Below this gauge a bend across a corner is a nuisance "
                "rather than a problem.",
            ),
            _spec("sheet_bend_over_length_eps_mm", "Bend runs off the panel past", TOL_MM),
        ),
    ),
    ThresholdGroup(
        "Sheet metal -- cut and formed features",
        "Punched and formed features, sized against the gauge for the same "
        "reason. A punch breaks below a size whatever the sheet is, and a "
        "countersink has to leave a land, so those two are absolute.",
        (
            _spec("sheet_min_hole_factor", "Punched hole diameter, minimum", GAUGE),
            _spec("sheet_hole_pitch_factor", "Hole to hole, minimum", GAUGE),
            _spec("sheet_hole_bend_clearance_factor", "Hole to bend, minimum", GAUGE),
            _spec("sheet_max_countersink_depth_factor", "Countersink depth, maximum", GAUGE),
            _spec("sheet_min_countersink_land_mm", "Land left under a countersink"),
            _spec("sheet_min_tab_width_mm", "Tab width, absolute minimum"),
            _spec("sheet_tab_width_factor", "Tab width, minimum", GAUGE),
            _spec("sheet_tab_max_aspect", "Tab length over width, maximum", RATIO),
            _spec("sheet_min_notch_factor", "Notch width, minimum", GAUGE),
            _spec("sheet_notch_max_depth_ratio", "Notch depth over width, maximum", RATIO),
            _spec("sheet_emboss_max_depth_factor", "Emboss depth, maximum", GAUGE),
            _spec(
                "sheet_louver_max_height_factor",
                "Louver hood height, maximum",
                GAUGE,
                decimals=4,
                note="The default is the tallest standard louver die: "
                "6.35 mm of hood on 1.897 mm stock.",
            ),
            _spec("sheet_formed_min_pitch_factor", "Formed feature to feature, minimum", GAUGE),
            _spec("sheet_formed_bend_clearance_factor", "Formed feature to bend, minimum", GAUGE),
            _spec("sheet_dimension_eps_mm", "Treat two sheet dimensions as equal within", TOL_MM),
        ),
    ),
    ThresholdGroup(
        "Tolerance and finish capability",
        "What the shop can hold, not what the drawing asks for. Dormant "
        "until a tolerance actually reaches the analysis from the model, but "
        "worth setting while the equipment list is fresh.",
        (
            _spec("tol_grinding_max_mm", "Tightest tolerance ground", TOL_MM),
            _spec("tol_lapping_max_mm", "Tightest tolerance lapped", TOL_MM),
            _spec("gdt_position_achievable_3axis_mm", "Position held, 3-axis", TOL_MM),
            _spec("gdt_position_achievable_3plus2_mm", "Position held, 3+2", TOL_MM),
            _spec("gdt_position_achievable_5axis_mm", "Position held, 5-axis", TOL_MM),
            _spec("gdt_form_achievable_3axis_mm", "Form held, 3-axis", TOL_MM),
            _spec("gdt_form_achievable_3plus2_mm", "Form held, 3+2", TOL_MM),
            _spec("gdt_form_achievable_5axis_mm", "Form held, 5-axis", TOL_MM),
            _spec("ra_lapping_um", "Surface finish lapped", UM),
            _spec("ra_grinding_um", "Surface finish ground", UM),
            _spec("ra_standard_mill_um", "Surface finish off the mill", UM),
        ),
    ),
    ThresholdGroup(
        "Recognition -- how the part is made",
        "These decide which branch every process-aware rule takes, so a "
        "wrong answer here is worse than a wrong limit: the part gets judged "
        "as the wrong sort of part. The gauge limits are press capacity -- "
        "heavier than the brake takes is plate to be machined, not sheet to "
        "be formed.",
        (
            _spec("turned_fraction_turned_min", "Turned area before the part is turned", SHARE),
            _spec("turned_fraction_milled_max", "Milled area allowed on a turned part", SHARE),
            _spec(
                "turned_convex_share_min",
                "Convex outside needed to call it turned",
                SHARE,
                note="What keeps a bored block from reading as a turned "
                "part: a turned part has a profile, not just a hole.",
            ),
            _spec("profile_bar_min_length_ratio", "Extrusion length over section", RATIO),
            _spec("sheet_classify_max_gauge_mm", "Heaviest gauge the brake takes"),
            _spec("sheet_fold_min_gauge_mm", "Zero-radius fold, thinnest gauge"),
            _spec("sheet_fold_max_gauge_mm", "Zero-radius fold, heaviest gauge"),
        ),
    ),
    ThresholdGroup(
        "Recognition -- what a feature is",
        "The windows a recognizer uses to name a feature. Widening one does "
        "not change a rule's verdict, it changes which rules get asked, so "
        "these are set to match the work the shop takes rather than tuned "
        "against a single part.",
        (
            _spec("draft_min_deg", "Draft, shallowest that counts", DEG),
            _spec("draft_max_deg", "Draft, steepest that counts", DEG),
            _spec("rib_recognized_max_thickness_mm", "Rib, thickest web that is still a rib"),
            _spec("rib_recognized_min_height_aspect", "Rib, height over thickness from", RATIO),
            _spec("flexure_slit_max_width_mm", "Flexure slit, widest"),
            _spec("flexure_slit_min_depth_ratio", "Flexure slit, depth over width from", RATIO),
            _spec("marking_max_depth_mm", "Marking, deepest cut"),
            _spec("marking_max_stroke_width_mm", "Marking, widest stroke"),
            _spec("marking_max_glyph_size_mm", "Marking, largest character"),
            _spec(
                "oring_gland_min_width_mm",
                "O-ring gland, narrowest",
                note="The defaults are AS568 cord sizes. A shop sealing to "
                "metric cord cuts a different band.",
            ),
            _spec("oring_gland_max_width_mm", "O-ring gland, widest"),
            _spec(
                "oring_gland_min_width_depth_ratio",
                "O-ring gland, width over depth from",
                RATIO,
            ),
            _spec(
                "oring_gland_max_width_depth_ratio",
                "O-ring gland, width over depth to",
                RATIO,
            ),
        ),
    ),
)


# =============================================================================
# Queries -- what the tests and the widgets both read
# =============================================================================


def preference_key(field: str) -> str:
    """The parameter-store key a threshold field is written under."""
    return THRESHOLD_PREF_PREFIX + field


def all_specs() -> tuple[ThresholdSpec, ...]:
    return tuple(spec for group in THRESHOLD_GROUPS for spec in group.specs)


def spec_for(field: str) -> Optional[ThresholdSpec]:
    for spec in all_specs():
        if spec.field == field:
            return spec
    return None


def threshold_field_names() -> tuple[str, ...]:
    """Every field `RuleThresholds` actually carries."""
    return tuple(f.name for f in dataclass_fields(RuleThresholds))


def unreachable_fields() -> tuple[str, ...]:
    """Threshold fields the editor cannot reach.

    Expected to be empty. A field added to `RuleThresholds` without a spec is
    a number a shop cannot set, which is the whole failure this page exists to
    prevent, so it is asserted rather than trusted.
    """
    covered = {spec.field for spec in all_specs()}
    return tuple(name for name in threshold_field_names() if name not in covered)


def default_for(field: str):
    """The value the rules start from, read off `RuleThresholds`."""
    return getattr(RuleThresholds(), field)


def is_integer_field(field: str) -> bool:
    """Whether the field counts things.

    Decided by the type of its own default, which is how
    `RuleThresholds.apply_overrides` decides it too. `from __future__ import
    annotations` makes the dataclass report annotations as strings, so the
    default is the only honest source -- and having one source is what keeps
    the spinbox type and the parameter setter from drifting apart.
    """
    value = default_for(field)
    return isinstance(value, int) and not isinstance(value, bool)


def common_field_names() -> tuple[str, ...]:
    return tuple(spec.field for spec in all_specs() if spec.common)


def preference_defaults() -> dict:
    """Every threshold key with its default, typed as it will be stored.

    A float default stays a float here so a round trip through the parameter
    store can be checked against it without the check itself rounding.
    """
    return {
        preference_key(spec.field): (
            int(default_for(spec.field))
            if is_integer_field(spec.field)
            else float(default_for(spec.field))
        )
        for spec in all_specs()
    }


# =============================================================================
# The page
# =============================================================================


def _display_label(spec: ThresholdSpec) -> str:
    """Bold marks the ones worth coming back to. Qt reads the markup."""
    return f"<b>{spec.label}</b>" if spec.common else spec.label


def threshold_panels() -> list:
    """One `AnalyzerPanel` per group, paired with the group, in page order.

    Qt is imported here rather than at the top of the module so the tables
    above can be read on a machine with no GUI stack installed.
    """
    from .preferences import AnalyzerPanel, FieldGroup, FloatField, IntField

    panels = []
    for group in THRESHOLD_GROUPS:
        widget_fields = []
        for spec in group.specs:
            style = spec.style
            key = preference_key(spec.field)
            if is_integer_field(spec.field):
                widget_fields.append(
                    IntField(
                        key,
                        _display_label(spec),
                        default=int(default_for(spec.field)),
                        min=int(style.minimum),
                        max=int(style.maximum),
                        suffix=style.suffix,
                        tooltip=spec.note,
                    )
                )
            else:
                widget_fields.append(
                    FloatField(
                        key,
                        _display_label(spec),
                        default=float(default_for(spec.field)),
                        min=style.minimum,
                        max=style.maximum,
                        step=style.step,
                        decimals=style.decimals,
                        suffix=style.suffix,
                        tooltip=spec.note,
                    )
                )

        panel_class = type(
            "MachiningThresholdPanel",
            (AnalyzerPanel,),
            {
                "title": group.title,
                "groups": [FieldGroup(group.title, widget_fields)],
            },
        )
        panels.append((group, panel_class()))

    return panels


class MachiningThresholds:
    """The Limits page in Edit -> Preferences -> DFM."""

    def __init__(self):
        from PySide6 import QtGui, QtWidgets

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Machining Limits")
        self.form.setWindowIcon(QtGui.QIcon(":/icons/dfm_analysis.svg"))

        layout = QtWidgets.QVBoxLayout(self.form)

        intro = QtWidgets.QLabel(
            "Every number the machining rules compare against, dealt out by "
            "concern. The ones in <b>bold</b> are those a shop revisits as "
            "its work changes; the rest are set once. Right-click any of them "
            "to put it back to the default."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.chooser = QtWidgets.QComboBox()
        self.stack = QtWidgets.QStackedWidget()
        self.panels = []

        for group, panel in threshold_panels():
            self.chooser.addItem(group.title)
            self.stack.addWidget(self._with_blurb(group, panel))
            self.panels.append(panel)

        self.chooser.currentIndexChanged.connect(self.stack.setCurrentIndex)

        # The longest group runs to sixteen rows, and the dialog does not
        # grow to suit it.
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.stack)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)

        layout.addWidget(self.chooser)
        layout.addWidget(scroll)

    @staticmethod
    def _with_blurb(group, panel):
        """Give the group's explanation a home above its rows."""
        from PySide6 import QtWidgets

        if not group.blurb:
            return panel

        blurb = QtWidgets.QLabel(group.blurb)
        blurb.setWordWrap(True)
        font = blurb.font()
        font.setItalic(True)
        blurb.setFont(font)

        # Index 1 puts it under the panel's own heading.
        panel.layout().insertWidget(1, blurb)
        return panel

    # -- persistence --------------------------------------------------------

    def loadSettings(self) -> None:
        import FreeCAD as App  # type: ignore

        params = App.ParamGet("User parameter:BaseApp/Preferences/Mod/DFM")
        for panel in self.panels:
            panel.load(params)

    def saveSettings(self) -> None:
        import FreeCAD as App  # type: ignore

        params = App.ParamGet("User parameter:BaseApp/Preferences/Mod/DFM")
        for panel in self.panels:
            panel.save(params)
