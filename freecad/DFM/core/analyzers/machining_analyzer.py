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

from OCP.gp import gp_Vec
from OCP.TopoDS import TopoDS_Shape

from ...core.base.base_analyzer import BaseAnalyzer
from ...core.machining.aag import SurfaceType
from ...core.machining.aag_builder import AagBuilder
from ...core.machining.config import MachiningConfig
from ...core.machining.context import MachiningContext
from ...core.machining.features import FeatureType, RecognitionResult
from ...core.machining.process_classifier import (
    PartProcessType,
    classify_part_process,
    refine_part_process_with_features,
)
from ...core.machining.recognizers import RECOGNIZER_PIPELINE, SHEET_PIPELINE
from ...core.machining.resolver import resolve
from ...core.machining.thread_sources import ThreadEvidence, thread_evidence_for
from ...core.registries import register_analyzer
from ...core.utils.geometry import EdgeIndex, FaceIndex


# The whole-part context is stored under one key. Rules read the context
# rather than a per-face measurement, because a machining finding is usually
# about a relationship between faces rather than a property of one.
CONTEXT_KEY = ("Part", 0)


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
        # helix search on its own, exactly as before.
        evidence = thread_evidence_for(kwargs.get("target_object"))
        recognition = self._recognize(
            graph, shape, part_process, check_abort, evidence
        )

        # The classification ran before recognition because the recognizers
        # need its verdict, so it decided on shape alone. Two of its answers
        # can only be checked once the features exist: a turned part with a
        # slot in it is a mill-turn part, and a shell that looked like sheet
        # is really milled if it holds anything cut into solid stock.
        part_process = refine_part_process_with_features(
            part_process,
            recognition.features,
            graph,
            shape,
            declared_blank=self.config.blank_form,
        )

        context = MachiningContext(
            shape=shape,
            graph=graph,
            face_index=face_index,
            config=self.config,
            part_process=part_process,
            recognition=recognition,
        )
        return {CONTEXT_KEY: context}

    def _recognize(
        self,
        graph,
        shape,
        part_process,
        check_abort,
        evidence: Optional[ThreadEvidence] = None,
    ) -> RecognitionResult:
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
        if part_process.type is PartProcessType.SHEET_METAL:
            # Each sheet pass is told only what the sheet passes before it
            # claimed, not what the machining passes did. They are looking
            # at the same faces on purpose: the whole point of this stage is
            # to say what the press did to metal the milling vocabulary has
            # already described some other way.
            sheet_claimed: set[int] = set()
            for recognizer_class in SHEET_PIPELINE:
                if check_abort and check_abort():
                    break
                recognizer = recognizer_class()
                recognizer.config = self.config
                recognizer.part_process = part_process
                recognizer.thread_evidence = evidence
                try:
                    found = recognizer.recognize(
                        graph, shape, sheet_claimed, settled.features
                    )
                except Exception as exc:
                    App.Console.PrintWarning(
                        f"DFM: {recognizer.name} failed on this shape: {exc}\n"
                    )
                    continue
                settled.extend(found)
                for feature in found:
                    if feature.type in _CLAIMS_AGAINST_FORMING:
                        sheet_claimed.update(feature.faces)
            _drop_bend_duplicate_fillets(settled)
            _supersede_formed_reads(settled, graph)

        return settled


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


#: What a later sheet pass must not tread on. A bend's faces are not
#: available to the outline pass, and neither a bend's nor a punched
#: outline's are available to the forming pass -- a hem lip read as a formed
#: hood is the failure this prevents.
_CLAIMS_AGAINST_FORMING = (FeatureType.BEND, FeatureType.TAB, FeatureType.NOTCH)

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
    FeatureType.SLOT,
    FeatureType.STEP,
    FeatureType.GROOVE,
    FeatureType.O_RING_GLAND,
    FeatureType.RETAINING_RING_GROOVE,
)

#: The outline vocabulary, which loses on adjacency rather than on the faces
#: themselves.
_OUTLINE_READS = (FeatureType.TAB, FeatureType.NOTCH)


def _supersede_formed_reads(recognition, graph) -> None:
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

    def superseded(feature) -> bool:
        if feature.type in _OUTLINE_READS:
            return any(face_id in adjacent for face_id in feature.faces)
        if feature.type in _SUPERSEDED_BY_FORMING:
            return any(face_id in formed_faces for face_id in feature.faces)
        return False

    recognition.features = [f for f in recognition.features if not superseded(f)]
