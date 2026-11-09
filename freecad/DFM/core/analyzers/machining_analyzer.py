# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The single analyzer behind every machining rule.

Building the adjacency graph and classifying the part is expensive and every
machining rule needs both, so this runs once and each rule reads the result.
The runner already caches analyzer output by id and shares it across every
check declaring the same ``required_analyzer_id``, so no orchestration
changes are needed to make that happen.
"""

from typing import Any, Callable, Optional

import FreeCAD as App  # type: ignore

from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Pnt, gp_Vec
from OCP.TopoDS import TopoDS_Shape

from ...core.base.base_analyzer import BaseAnalyzer
from ...core.machining.aag import SurfaceType
from ...core.machining.aag_builder import AagBuilder
from ...core.machining.config import MachiningConfig
from ...core.machining.context import MachiningContext
from ...core.machining.features import FeatureType, RecognitionResult
from ...core.machining.process_classifier import (
    PartProcessResult,
    PartProcessType,
    classify_part_process,
    refine_part_process_with_features,
)
from ...core.machining.recognizers import RECOGNIZER_PIPELINE
from ...core.machining.recognizers.bend_recognizer import (
    BendRecognizer,
    append_sharp_fold_bends,
    drop_sheet_skin_machining_reads,
)
from ...core.machining.recognizers.sheet_formed_recognizer import (
    SheetFormedRecognizer,
    drop_profile_wall_marking,
    formed_claimed_faces,
)
from ...core.machining.recognizers.sheet_outline_recognizer import (
    SheetOutlineRecognizer,
)
from ...core.machining.recognizers.through_cavity_recognizer import (
    ThroughCavityRecognizer,
)
from ...core.machining.resolver import resolve
from ...core.machining.thread_sources import ThreadEvidence, thread_evidence_for
from ...core.registries import register_analyzer
from ...core.utils.geometry import EdgeIndex, FaceIndex


# The whole-part context is stored under one key. Rules read the context
# rather than a per-face measurement, because a machining finding is usually
# about a relationship between faces rather than a property of one.
CONTEXT_KEY = ("Part", 0)


# The reference's STEP import meshes every part before anything measures it
# (XdeProcessor::compute_model_hash, xde_processor.cpp:878): linear deflection
# 1.0, not relative, angular 0.5.
_MESH_DEFLECTION = 1.0
_MESH_ANGLE = 0.5


def _as_the_reference_reads_it(shape: TopoDS_Shape, face_index: FaceIndex):
    """A copy of the shape carrying the reference's triangulation.

    The reference meshes the part on import, and from then on every
    ``BRepBndLib::Add`` on it -- every face box in the adjacency graph, every
    part envelope a rule draws -- is built from that triangulation rather
    than from the analytic surfaces: tight on a spline face, padded by the
    deflection on the rest. Parity on anything a box decides needs the same.

    A copy, because the document's shape is not ours to change. And a copy
    with its triangulation dropped before meshing: a FreeCAD shape usually
    carries a display mesh at the user's own deflection, and meshing keeps a
    finer existing mesh, which would make the findings depend on the view
    settings. Copying keeps the topology, so face ids are the caller's.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools

    try:
        copy = BRepBuilderAPI_Copy(shape, True, False).Shape()
        BRepTools.Clean_s(copy)
        BRepMesh_IncrementalMesh(copy, _MESH_DEFLECTION, False, _MESH_ANGLE, False)
    except Exception:
        return shape, face_index
    copied = FaceIndex(copy)
    if len(copied) != len(face_index):
        return shape, face_index
    return copy, copied


@register_analyzer("MACHINING_ANALYZER")
class MachiningAnalyzer(BaseAnalyzer):
    """Builds the adjacency graph and classifies the manufacturing process."""

    @property
    def analysis_type(self) -> str:
        return "MACHINING_ANALYZER"

    @property
    def name(self) -> str:
        return "Machining Analyzer"

    def resolve_prefs(self, prefs: dict) -> None:
        self.config = MachiningConfig.from_preferences(prefs)

    def execute(
        self,
        shape: TopoDS_Shape,
        face_index: FaceIndex,
        edge_index: EdgeIndex,
        progress_cb: Optional[Callable[[int], None]] = None,
        check_abort: Optional[Callable[[], bool]] = None,
        **kwargs: Any,
    ) -> dict[tuple[str, int], Any]:
        self.resolve_prefs(kwargs.get("prefs", {}))

        # Everything below reads a meshed copy, never the caller's shape.
        shape, face_index = _as_the_reference_reads_it(shape, face_index)

        graph = AagBuilder(shape, face_index).build()
        if progress_cb:
            progress_cb(len(face_index))
        if check_abort and check_abort():
            return {}

        part_process = classify_part_process(graph, self.config.thresholds, shape)
        # Read before recognition, because a bore the document calls tapped
        # has to be one when the hole pass sees it. The target object is what
        # carries the answer; an analysis handed nothing but a shape -- a
        # test, or a caller with no document -- gets empty evidence and the
        # bore's own spline edges to go on, exactly as before.
        evidence = thread_evidence_for(kwargs.get("target_object"))
        # A STEP file's PMI can link a thread callout to the bore it names;
        # the hole pass reads that before any geometric guess.
        pmi_threads = getattr(kwargs.get("annotations"), "threads", None)
        if pmi_threads:
            evidence.pmi = dict(pmi_threads)
        recognition, part_process = self._recognize(
            graph, shape, part_process, check_abort, evidence
        )

        context = MachiningContext(
            shape=shape,
            graph=graph,
            face_index=face_index,
            config=self.config,
            part_process=part_process,
            recognition=recognition,
            # Tolerance and finish callouts that came with the shape -- the
            # PMI of a STEP file, read alongside it. Absent for a document
            # part, which then reads exactly as before.
            annotations=kwargs.get("annotations"),
        )
        return {CONTEXT_KEY: context}

    def _recognize(
        self,
        graph,
        shape,
        part_process,
        check_abort,
        evidence: Optional[ThreadEvidence] = None,
    ) -> tuple[RecognitionResult, PartProcessResult]:
        """Run the recognizers in order, each told what the others claimed.

        The order is load-bearing: a later recognizer is given the faces
        already spoken for, so a groove does not re-recognize the bore it
        sits in.

        The shop configuration, the part classification and whatever the
        document states about the part's threads are set on each recognizer
        rather than passed as arguments. Most recognizers need none of the
        three, and threading three more parameters through every signature to
        serve the two that do would be all cost and no clarity.

        What comes out of the pipeline is raw. The recognizers are eager and
        cannot see each other's work, so the same faces get claimed several
        times over -- correctly each time, and only one of those readings is
        what the shop is going to do. The resolver settles that.
        """
        result = RecognitionResult()
        claimed: set[int] = set()
        if evidence is None:
            evidence = ThreadEvidence()

        for recognizer_class in RECOGNIZER_PIPELINE:
            if check_abort and check_abort():
                break
            recognizer = recognizer_class()
            recognizer.config = self.config
            recognizer.part_process = part_process
            recognizer.thread_evidence = evidence
            try:
                found = recognizer.recognize(graph, shape, claimed, result.features)
            except Exception as exc:  # one bad recognizer must not lose the rest
                App.Console.PrintWarning(
                    f"DFM: {recognizer.name} failed on this shape: {exc}\n"
                )
                continue
            result.extend(found)
            for feature in found:
                claimed.update(feature.faces)

        # Two corrections before the resolver sees any of it. Both are
        # about what the pocket pass could not have known: it runs early,
        # one cavity at a time, and some of what it says only reads wrong
        # once the rest of the part has been recognized.
        _merge_port_interrupted_pockets(result.features, graph)
        _drop_undercut_dominated_pockets(result.features)

        settled = resolve(result.features, graph)

        # The classification ran before recognition because the recognizers
        # need its verdict, so it decided on shape alone. Two of its answers
        # can only be checked once the features exist: a turned part with a
        # slot in it is a mill-turn part, and a shell that looked like sheet
        # is really milled if it holds anything cut into solid stock.
        #
        # It has to be settled here rather than after this method returns,
        # because the sheet stage below turns on the answer. Asking the
        # first, shape-only verdict instead put punched notches and formed
        # louvers on parts the refinement then went on to call milled.
        part_process = refine_part_process_with_features(
            part_process,
            settled.features,
            graph,
            shape,
            declared_blank=self.config.blank_form,
        )

        # The sheet-metal pass runs after the resolver rather than inside it,
        # and appends what it finds.
        #
        # It has to run late: a bend can only be recognized once the final,
        # feature-veto-refined classification says sheet metal, and that is
        # not known until resolution is done. Running it after also means a
        # part the classifier did not call sheet metal can never pick up a
        # bend, which keeps every milled and turned result untouched.
        #
        # The cost of running late is that the blend pass has already been
        # and gone, and it reported both of the bend's skins as fillets --
        # they are tangent cylinders, which is precisely a fillet. Those
        # duplicates are cleaned up below rather than prevented, because at
        # the time the blends were found there was nothing to prevent them
        # with.
        if (
            part_process.type is PartProcessType.SHEET_METAL
            and part_process.sheet_thickness_mm > 1.0e-6
        ):
            self._run_sheet_stage(graph, shape, part_process, settled, check_abort, evidence)

        # One face, one entry. Merges and multi-path collectors can list a
        # face twice, which inflates every face-based count downstream. Done
        # here because this is the only point every producer has finished;
        # first occurrence wins, since faces[0] is a feature's primary face.
        for feature in settled.features:
            if len(feature.faces) > 1 and len(set(feature.faces)) != len(feature.faces):
                feature.faces = list(dict.fromkeys(feature.faces))

        return settled, part_process


    def _run_sheet_stage(
        self, graph, shape, part_process, settled, check_abort, evidence
    ) -> None:
        """The sheet-metal stage, in the reference's one sheet pipeline order.

        Each sheet pass is told only what the sheet passes before it claimed,
        not what the machining passes did: they look at the same faces on
        purpose, because this stage exists to say what the press did to metal
        the milling vocabulary has already described some other way.
        """
        gauge = part_process.sheet_thickness_mm

        def run(recognizer_class, claimed: set[int]) -> list:
            recognizer = recognizer_class()
            recognizer.config = self.config
            recognizer.part_process = part_process
            recognizer.thread_evidence = evidence
            try:
                return recognizer.recognize(graph, shape, claimed, settled.features)
            except Exception as exc:
                App.Console.PrintWarning(
                    f"DFM: {recognizer.name} failed on this shape: {exc}\n"
                )
                return []

        # FIRST, retract the markings that are cut walls -- before anything
        # builds a claimed set from the feature list. See
        # drop_profile_wall_marking.
        settled.features = drop_profile_wall_marking(settled.features, graph, gauge)

        if check_abort and check_abort():
            return
        bends = run(BendRecognizer, set())
        settled.extend(bends)
        # Every bend's two skins were also reported as fillets by the blend
        # pass, which ran before the part was known to be sheet.
        _drop_bend_duplicate_fillets(settled)
        # And the machining reads of a skin the bend pass did NOT pair (a
        # rolled scroll's tip arc read as a bore). Here, so the phantom never
        # reaches the outline/formed claim sets, the blank purge or the
        # invisible-fold census.
        settled.features = drop_sheet_skin_machining_reads(settled.features, graph, gauge)

        # Outline tabs and notches, told only what the bends claimed.
        if check_abort and check_abort():
            return
        bend_faces = {face_id for feature in bends for face_id in feature.faces}
        settled.extend(run(SheetOutlineRecognizer, bend_faces))

        # Formed features, after the bend and outline claims so a hem lip is
        # never misread as a hood. Their machining reads are superseded.
        if check_abort and check_abort():
            return
        formed = run(SheetFormedRecognizer, formed_claimed_faces(settled.features, graph, gauge))
        settled.extend(formed)
        _supersede_formed_reads(settled, graph, gauge)

        # Family four of the fold census: zero-radius folds. After the formed
        # pass, because the detector must leave out the faces a formed hood
        # owns -- its wall-to-crest corner is coined sharp by design. A sharp
        # fold claims no faces, so nothing below trips on it.
        append_sharp_fold_bends(settled.features, graph, gauge)
        _drop_blank_reads(settled, graph, shape, gauge)
        _drop_hem_gaps(settled, graph, gauge)
        _drop_steps(settled)


def _drop_bend_duplicate_fillets(recognition) -> None:
    """Remove the fillets that are a bend seen a second time.

    A bend has an inner radius face and an outer one a gauge thickness
    further out, and both are cylinders tangent to the panels they join --
    which is a fillet's signature exactly. The blend pass runs long before
    the part is known to be sheet, so it cannot know a bend is coming, and
    every fold ends up reported three times: once as the bend, twice as the
    fillets its skins make.

    Matched on face identity and never on radius. A fillet that merely
    happens to share a radius with a bend, somewhere else on the part, is a
    real fillet with a real tool behind it.

    A blend that claims a bend skin *and* something else is left whole. The
    blend pass merges same-radius faces, so such a feature still describes
    real non-bend geometry, and its radius was measured from whichever
    member has the most area -- possibly that one. Trimming its face list
    would leave a feature whose numbers describe geometry it no longer
    claims. A duplicate left standing is the safer error.

    Sheet metal only. On a milled part a fillet carries rules of its own,
    and this never runs there.
    """
    bend_faces: set[int] = set()
    for feature in recognition.features:
        if feature.type == FeatureType.BEND:
            bend_faces.update(feature.faces)
    if not bend_faces:
        return

    recognition.features = [
        feature
        for feature in recognition.features
        if not (
            feature.type == FeatureType.FILLET
            and feature.faces
            and all(face_id in bend_faces for face_id in feature.faces)
        )
    ]

def _merge_port_interrupted_pockets(features, graph) -> None:
    """Rejoin one channel that drilled ports broke into pockets.

    A manifold's channel with feed holes down through its floor arrives here
    as one pocket per stretch between holes: the pocket pass stops at each
    port because a cylinder is not a wall it can grow through. Sixteen
    fragments where a machinist sees four channels, each of them then
    reported on separately -- sixteen narrow-opening warnings about one
    cutter path.

    Two fragments belong together when a port pierces both of them, their
    floors face the same way, and those floors are the same floor rather
    than two at different heights that a long hole happens to pass through.
    """
    class _Pocket:
        __slots__ = ("index", "normal", "centroid", "ports")

    grouped: list[_Pocket] = []
    for index, feature in enumerate(features):
        if feature.type != FeatureType.POCKET or not feature.faces:
            continue
        floor_id = feature.faces[0]  # the pocket pass puts its floor first
        if not graph.has_node(floor_id):
            continue
        floor = graph.node(floor_id)
        if floor.surface_type is not SurfaceType.PLANE:
            continue
        normal = floor.outward_normal
        if normal is None:
            continue

        ports = []
        for edge in graph.edges_of(floor_id):
            node = graph.node(edge.other_face(floor_id))
            if node.surface_type is not SurfaceType.CYLINDER:
                continue
            if node.cyl_cone_axis is None:
                continue
            if abs(node.cyl_cone_axis.Direction().Dot(normal)) > 0.95:
                ports.append(node.face_id)
        if not ports:
            continue

        entry = _Pocket()
        entry.index = index
        entry.normal = normal
        entry.centroid = floor.centroid
        entry.ports = ports
        grouped.append(entry)

    if len(grouped) < 2:
        return

    parent = list(range(len(grouped)))

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_port: dict[int, list[int]] = {}
    for position, entry in enumerate(grouped):
        for port in entry.ports:
            by_port.setdefault(port, []).append(position)

    for sharing in by_port.values():
        if len(sharing) < 2:
            continue
        first = grouped[sharing[0]]
        for other in sharing[1:]:
            second = grouped[other]
            if first.normal.Dot(second.normal) < 0.95:
                continue
            offset = gp_Vec(first.centroid, second.centroid)
            if abs(offset.Dot(gp_Vec(first.normal))) > 0.1:
                continue  # two floors at different heights, one long hole
            a, b = root(sharing[0]), root(other)
            if a != b:
                parent[a] = b

    families: dict[int, list[int]] = {}
    for position in range(len(grouped)):
        families.setdefault(root(position), []).append(position)

    absorbed: set[int] = set()
    for family in families.values():
        if len(family) < 2:
            continue
        family.sort(key=lambda position: grouped[position].index)
        keeper = features[grouped[family[0]].index]
        faces = list(keeper.faces)
        seen = set(faces)
        for position in family[1:]:
            index = grouped[position].index
            for face_id in features[index].faces:
                if face_id not in seen:
                    seen.add(face_id)
                    faces.append(face_id)
            absorbed.add(index)
        keeper.faces = faces

    if absorbed:
        features[:] = [f for i, f in enumerate(features) if i not in absorbed]


def _drop_undercut_dominated_pockets(features) -> None:
    """Let the undercut have the cavities that are nothing but undercut.

    The pocket pass runs before anything has asked what a tool could reach,
    so a mirror seat or a dovetail bottom is recognized as a cavity like any
    other. Once the undercut pass has claimed the same faces, every rule
    that fires on a pocket fires alongside the one finding that actually
    matters -- that a three-axis cutter cannot get in there.

    Only where the cavity is essentially all undercut, and only where its
    floor is tilted. A cavity whose floor is square to an axis and yet
    unreachable everywhere is a sealed void, which is one error about the
    whole cavity rather than six about its walls, and dropping the pocket
    would trade the one for the six.
    """
    undercut_faces: set[int] = set()
    for feature in features:
        if feature.type == FeatureType.UNDERCUT:
            undercut_faces.update(feature.faces)
    if not undercut_faces:
        return

    doomed: set[int] = set()
    for index, feature in enumerate(features):
        if feature.type != FeatureType.POCKET or not feature.faces:
            continue
        inside = sum(1 for face_id in feature.faces if face_id in undercut_faces)
        if inside <= len(feature.faces) * 0.8:
            continue
        normal = feature.parameters.get("floor_normal")
        if isinstance(normal, (list, tuple)) and len(normal) == 3:
            if max(abs(float(v)) for v in normal) > 0.95:
                continue  # square to an axis: a sealed void, not an undercut
        doomed.add(index)

    if doomed:
        features[:] = [f for i, f in enumerate(features) if i not in doomed]


#: The machining vocabulary a formed feature speaks over. An emboss pressed
#: into a panel is a raised dome on one side and the same dome hollow on the
#: other, so the milling passes see a boss and a pocket and are each right
#: about the shape and wrong about the part: nobody milled either of them,
#: and the rules that would fire -- corner radii, depth ratios, floor finish
#: -- are about a cutter that was never there.
_SUPERSEDED_BY_FORMING = (
    FeatureType.BOSS,
    FeatureType.POCKET,
    FeatureType.SPHERICAL_POCKET,
    FeatureType.BLIND_HOLE,
    FeatureType.BREAKTHROUGH_HOLE,
    FeatureType.SLOT,
    FeatureType.STEP,
    FeatureType.THROUGH_CAVITY,
    # An undercut read of formed geometry is a tool-access warning about
    # metal no tool touches. It appears when the form faces a direction the
    # cardinal probes cannot reach (hoods on a vertical wall opening
    # downward); whether a punch can form it is the sheet_formed_* rules'
    # question.
    FeatureType.UNDERCUT,
    FeatureType.GROOVE,
    FeatureType.O_RING_GLAND,
    FeatureType.RETAINING_RING_GROOVE,
    # A formed feature's corner radii belong to the punch, not to a deburring
    # pass: an obround emboss's rounded ends (the outer wall and the inner
    # wall one gauge behind it) come back from the blend pass as fillets.
    # Same argument as the bend duplicates; a milled fillet survives, since
    # its face has to be one the form itself claims.
    FeatureType.FILLET,
    FeatureType.CHAMFER,
)

#: The outline vocabulary, which loses on adjacency rather than on the faces
#: themselves.
_OUTLINE_READS = (FeatureType.TAB, FeatureType.NOTCH)


def _drop_hem_gaps(recognition, graph, gauge: float) -> None:
    """Drop the THROUGH_CAVITY that is a hem's own gap.

    An open or teardrop hem folds the metal back alongside the panel and
    leaves a slot between the return and the wall, one gauge wide and as long
    as the hem. The read is geometrically true, but the hem BEND already
    describes the fold and sheet_hem_dimensions judges the gap. Gated
    narrowly, since a genuine cutout through a hemmed panel looks similar:
    the cavity must be gauge-wide AND touch a hem.
    """
    hem_adjacent: set[int] = set()
    for feature in recognition.features:
        if feature.type != FeatureType.BEND:
            continue
        if not feature.parameters.get("is_hem", False):
            continue
        for face_id in feature.faces:
            hem_adjacent.add(face_id)
            if graph.has_node(face_id):
                hem_adjacent.update(graph.neighbors_of(face_id))
    if not hem_adjacent or gauge <= 0.0:
        return

    def hem_gap(feature) -> bool:
        if feature.type != FeatureType.THROUGH_CAVITY:
            return False
        width = float(feature.parameters.get("width_mm", 0.0) or 0.0)
        if abs(width - gauge) > 0.1:
            return False
        return any(face_id in hem_adjacent for face_id in feature.faces)

    recognition.features = [f for f in recognition.features if not hem_gap(f)]


def _drop_blank_reads(recognition, graph, shape, gauge: float) -> None:
    """Drop machining reads of the blank's own geometry on a sheet part.

    RIB: a rib is a cast or milled stiffener standing proud of a wall. Sheet
    stiffness comes from bends, beads and embosses, which have types of their
    own; every rib read on a sheet part is a flange, a hem return or a
    channel wall.

    FLEXURE_SLIT, only when every face of it already belongs to a TAB or
    NOTCH: one cut, two names. A slit that is not an outline feature may be
    a real spring-steel flexure and is left alone.

    A FLEXURE_SLIT narrower than the gauge is shear residue -- the cusp two
    profile cuts leave where they meet. No laser kerf, punch or wire makes a
    slit thinner than the material, and a flexure needs the width to survive
    being bent.

    A CHAMFER on the blank's own edge: every face of it bridges the gauge, so
    it is the profile cut square through the sheet and its "distance" is
    just the thickness. Chamfers on formed or machined geometry survive. A
    chamfer under a fifth of the gauge goes too, as boolean residue.

    A SLOT or POCKET deeper than 2.5 gauges: the blank's cut walls read as
    something else.

    A HOLE whose wall does not close. A drill makes a full circle; on a sheet
    part everything is cut by one profile operation, so a cylindrical wall
    covering only an arc (a scallop relief, a keyhole's end circle open into
    its slot) is a piece of that profile. Sheet only, deliberately: on a
    machined part an open arc is usually a real bore something else
    intersected. BREAKTHROUGH and PARTIAL_BORE reads are exempt by
    definition.
    """
    outline_faces: set[int] = set()
    for feature in recognition.features:
        if feature.type in (FeatureType.TAB, FeatureType.NOTCH):
            outline_faces.update(feature.faces)

    def profile_edge(face_id: int) -> bool:
        # The same bridges-the-gauge test the outline recognizer finds its
        # strips with: one gauge deep, both skins among its neighbours.
        if not graph.has_node(face_id):
            return False
        node = graph.node(face_id)
        if node.surface_type is not SurfaceType.PLANE:
            return False
        dims = sorted(node.bbox_dims())
        if dims[1] > gauge * 1.3 or dims[1] < gauge * 0.5:
            return False
        normal = node.outward_normal
        if normal is None:
            return False
        sides: list = []
        for neighbour_id in graph.neighbors_of(face_id):
            if not graph.has_node(neighbour_id):
                continue
            neighbour = graph.node(neighbour_id)
            if neighbour.surface_type is not SurfaceType.PLANE:
                continue
            other = neighbour.outward_normal
            if other is None or abs(other.Dot(normal)) > 0.3:
                continue
            if any(previous.Dot(other) < -0.9 for previous in sides):
                return True
            sides.append(other)
        return False

    def wall_closes(feature) -> bool:
        # A full circle spans about 2r in both directions across its axis;
        # 1.9r leaves room for the padding OCCT adds to a curved face's box.
        if not feature.faces or not graph.has_node(feature.faces[0]):
            return True
        seed = graph.node(feature.faces[0])
        if seed.surface_type is not SurfaceType.CYLINDER or seed.cyl_cone_axis is None:
            return True
        axis = seed.cyl_cone_axis.Direction()
        wall = Bnd_Box()
        for face_id in feature.faces:
            if not graph.has_node(face_id):
                continue
            node = graph.node(face_id)
            if node.surface_type is not SurfaceType.CYLINDER or node.cyl_cone_axis is None:
                continue
            if abs(node.cyl_radius - seed.cyl_radius) > 1.0e-3:
                continue
            if abs(node.cyl_cone_axis.Direction().Dot(axis)) < 0.99:
                continue
            if not node.bbox.IsVoid():
                wall.Add(node.bbox)
        if wall.IsVoid():
            return True
        # ACROSS THE AXIS, not across the world. Reading the two world-axis
        # spans that are not the axis's own is the same question only when
        # the axis is axis-aligned; a bore on a 45-degree flange splits its
        # circle between two world axes and failed both. Project the box's
        # corners onto a frame perpendicular to the axis instead. A box
        # over-estimates an oblique circle, so this can only be more
        # permissive -- it cannot invent a closure that is not there.
        ax = gp_Vec(axis)
        seed_vec = gp_Vec(0.0, 0.0, 1.0) if abs(axis.Z()) < 0.9 else gp_Vec(1.0, 0.0, 0.0)
        u = ax.Crossed(seed_vec)
        if u.Magnitude() < 1.0e-9:
            return True
        u.Normalize()
        v = ax.Crossed(u)
        if v.Magnitude() < 1.0e-9:
            return True
        v.Normalize()
        xn, yn, zn, xx, yx, zx = wall.Get()
        ulo = vlo = 1.0e18
        uhi = vhi = -1.0e18
        for x in (xn, xx):
            for y in (yn, yx):
                for z in (zn, zx):
                    p = gp_Vec(x, y, z)
                    du, dv = p.Dot(u), p.Dot(v)
                    ulo, uhi = min(ulo, du), max(uhi, du)
                    vlo, vhi = min(vlo, dv), max(vhi, dv)
        need = 1.9 * seed.cyl_radius
        return (uhi - ulo) >= need and (vhi - vlo) >= need

    def on_blank_outline(face_id: int) -> bool:
        # A sheet part's outer profile is cut in one pass, so its concavities
        # and corner radii are the shape of the blank, not features anyone
        # made one at a time -- the same reason a casting's envelope faces
        # are claimed by nothing. Interior cutouts stay, and the discriminant
        # needs no threshold: a wall cut THROUGH the sheet hangs off an inner
        # wire on both skins it passes between, the outline meets them
        # across the outer one.
        if not graph.has_node(face_id):
            return False
        touches_plane = False
        for edge in graph.edges_of(face_id):
            if edge.is_inner_wire_edge:
                return False
            other = edge.other_face(face_id)
            if graph.has_node(other) and graph.node(other).surface_type is SurfaceType.PLANE:
                touches_plane = True
        return touches_plane

    orphaned_arcs: set[int] = set()

    def blank_read(feature) -> bool:
        # Profile geometry, whatever it was named.
        if feature.type in (FeatureType.FILLET, FeatureType.THROUGH_CAVITY):
            if not feature.faces:
                return False
            return all(on_blank_outline(face_id) for face_id in feature.faces)
        if feature.type == FeatureType.RIB:
            return True
        if feature.type in _HOLES_THAT_CLOSE:
            if wall_closes(feature):
                return False
            orphaned_arcs.update(feature.faces)
            return True
        if feature.type == FeatureType.CHAMFER:
            if not feature.faces:
                return False
            # Far smaller than the gauge it is boolean residue: a ledge left
            # where one cut ran out against another, which nothing made.
            distance = float(feature.parameters.get("distance_mm", gauge))
            if distance < gauge * 0.2:
                return True
            # As deep as the material it is not a chamfer either: an edge
            # break by the full thickness leaves nothing to break. It is the
            # sheared profile edge, whose "distance" is the gauge it spans,
            # and it slips the face test below because a profile feature
            # mixes wall strips with faces lying flat on the skin. The 0.05
            # is float slop on a gauge read back from geometry, not a margin.
            if distance >= gauge - 0.05:
                return True
            # ASK THE WIRE, NOT THE SIZE. The two bars above are absolute and
            # leak: a profile "chamfer" at 3.0378 on a 3.1767 gauge clears the
            # second by 0.089. The question is topological and already
            # answered for FILLET and THROUGH_CAVITY -- a wall cut through the
            # sheet hangs off an inner wire, the blank's own profile meets the
            # skins across the outer one. ANY face on the outline disqualifies
            # the claim, not ALL: blend-network merging pulls corner faces
            # into one chamfer, so one non-profile face kept the rest alive. A
            # real sheet chamfer (a countersink) is conical and carries
            # inner-wire edges, so it never satisfies this.
            if any(on_blank_outline(face_id) for face_id in feature.faces):
                return True
            return all(profile_edge(face_id) for face_id in feature.faces)
        if feature.type in (FeatureType.SLOT, FeatureType.POCKET):
            # A recess deeper than the material cannot exist: a cut through
            # one gauge is a cutout, and there is nothing to sink into. These
            # are open space beside the blank where it was trimmed back.
            # 2.5 gauges, so a hem (double material) never trips it.
            return float(feature.parameters.get("depth_mm", 0.0)) > gauge * 2.5
        # MARKING ON A CUT WALL is handled at the top of the sheet stage now,
        # by drop_profile_wall_marking(). Here it ran after the formed pass had
        # already been handed a claimed set holding the bogus marking. Do not
        # re-add it here.
        if feature.type != FeatureType.FLEXURE_SLIT or not feature.faces:
            return False
        if float(feature.parameters.get("width_mm", gauge)) < gauge:
            return True
        return all(face_id in outline_faces for face_id in feature.faces)

    recognition.features = [f for f in recognition.features if not blank_read(f)]

    # Give the aperture back its arc. A keyhole mount is a slot plus a wider
    # circle cut in one pass, but the hole pass reached the circle first and
    # the resolver ranks a through hole above a through cavity, so the cavity
    # lost the overlap before the test above deleted the bore that beat it.
    # That ranking is right for machined parts and the hole pass runs before
    # the part is known to be sheet, so re-run the cavity pass here and take
    # back only what this block orphaned: a cavity must use a face of a hole
    # just dropped, and none of a surviving feature's.
    if orphaned_arcs:
        live: set[int] = set()
        for feature in recognition.features:
            live.update(feature.faces)
        for cavity in ThroughCavityRecognizer().recognize(graph, shape):
            faces = set(cavity.faces)
            if faces & orphaned_arcs and not faces & live:
                live.update(faces)
                recognition.features.append(cavity)


def _drop_steps(recognition) -> None:
    """Drop every STEP on a sheet part.

    A milled shoulder is made by a cutter that stopped part-way down, and in
    sheet stock a cut goes through the blank or does not exist. Every STEP
    here is a read of something else -- a corner relief read once per
    adjacent wall, a bend's floor panel paired with a wall, a boolean sliver.
    A gauge-height test was tried first and its survivors proved it too
    narrow. No rule reads STEP.
    """
    recognition.features = [
        f for f in recognition.features if f.type != FeatureType.STEP
    ]


#: Hole reads whose wall has to close on a sheet part.
_HOLES_THAT_CLOSE = (
    FeatureType.THROUGH_HOLE,
    FeatureType.BLIND_HOLE,
    FeatureType.COUNTERBORE,
    FeatureType.COUNTERSINK,
    FeatureType.THREADED_HOLE,
)


def _supersede_formed_reads(recognition, graph, gauge: float) -> None:
    """Drop the milled reading of anything the press formed.

    A louver, an emboss, a lance: each is one operation on one piece of
    metal, and each shows the machining passes something they know how to
    name. The formed feature is the true account, so the others go.

    Punched tabs and notches go on adjacency rather than on the faces
    themselves. A louver's shear opening touches the hood without being part
    of it, and read on its own that opening is a notch in the outline -- but
    it is the louver's own opening, made by the same hit of the same tool,
    and nobody punched it separately.
    """
    formed_faces: set[int] = set()
    for feature in recognition.features:
        if feature.type == FeatureType.SHEET_FORMED:
            formed_faces.update(feature.faces)
    if not formed_faces:
        return

    adjacent = set(formed_faces)
    for face_id in formed_faces:
        if graph.has_node(face_id):
            adjacent.update(graph.neighbors_of(face_id))

    # A machining read lying under a formed feature's footprint, and touching
    # it, is that feature's own opening -- the TAB/NOTCH argument again. A
    # louver's shear mouth came back as a SLOT and a STEP filling exactly the
    # hood's plan area in the sheet's thickness, or as a THROUGH_CAVITY.
    # Containment as well as contact keeps a genuine slot that merely runs
    # past a form alive.
    formed_boxes: list[Bnd_Box] = []
    max_form_face = 0.0
    for feature in recognition.features:
        if feature.type != FeatureType.SHEET_FORMED:
            continue
        box = Bnd_Box()
        for face_id in feature.faces:
            if graph.has_node(face_id):
                node = graph.node(face_id)
                box.Add(node.bbox)
                max_form_face = max(max_form_face, node.area)
        if not box.IsVoid():
            # By the gauge: the mouth lies in the sheet's thickness directly
            # under the hood lanced out of it, so the two boxes meet edge-on
            # rather than nest. The box is only ever read grown.
            box.Enlarge(gauge + 0.2)
            formed_boxes.append(box)

    def under_a_form(feature) -> bool:
        # The candidate's host panel is left out of both tests: a SLOT or
        # STEP names the panel it sits in, which is neither adjacent to nor
        # inside the form. What must lie under the form is its walls and
        # floor -- and only SOME of it has to touch, because a mouth's far
        # wall meets only the sheet.
        #
        # Containment is tested per FACE against any form, not once on the
        # feature's combined box. For a feature inside one form the two are
        # the same question; they differ when one feature spans two forms --
        # two louver lips read as a single UNDERCUT, whose combined box
        # stretches across the deck between the hoods and sits in neither.
        own_faces = touching = covered = 0
        for face_id in feature.faces:
            if not graph.has_node(face_id):
                continue
            node = graph.node(face_id)
            if node.area > max_form_face * 1.5:
                continue  # the host
            if face_id in adjacent:
                touching += 1
            own_faces += 1
            if node.bbox.IsVoid():
                continue
            xn, yn, zn, xx, yx, zx = node.bbox.Get()
            low, high = gp_Pnt(xn, yn, zn), gp_Pnt(xx, yx, zx)
            if any(
                not box.IsOut(low) and not box.IsOut(high) for box in formed_boxes
            ):
                covered += 1
        if own_faces == 0 or touching == 0:
            return False
        return covered == own_faces

    def superseded(feature) -> bool:
        if feature.type in _OUTLINE_READS:
            return any(face_id in adjacent for face_id in feature.faces)
        if feature.type in _SUPERSEDED_BY_FORMING:
            if any(face_id in formed_faces for face_id in feature.faces):
                return True
            return under_a_form(feature)
        return False

    recognition.features = [f for f in recognition.features if not superseded(f)]
