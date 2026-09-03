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

from OCP.TopoDS import TopoDS_Shape

from ...core.base.base_analyzer import BaseAnalyzer
from ...core.machining.aag_builder import AagBuilder
from ...core.machining.config import MachiningConfig
from ...core.machining.context import MachiningContext
from ...core.machining.features import RecognitionResult
from ...core.machining.process_classifier import classify_part_process
from ...core.machining.recognizers import RECOGNIZER_PIPELINE
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
        recognition = self._recognize(graph, shape, part_process, check_abort)

        context = MachiningContext(
            shape=shape,
            graph=graph,
            face_index=face_index,
            config=self.config,
            part_process=part_process,
            recognition=recognition,
        )
        return {CONTEXT_KEY: context}

    def _recognize(self, graph, shape, part_process, check_abort) -> RecognitionResult:
        """Run the recognizers in order, each told what the others claimed.

        The order is load-bearing: a later recognizer is given the faces
        already spoken for, so a groove does not re-recognize the bore it
        sits in.

        The shop configuration and the part classification are set on each
        recognizer rather than passed as arguments. Most recognizers need
        neither, and threading two more parameters through every signature to
        serve the two that do would be all cost and no clarity.
        """
        result = RecognitionResult()
        claimed: set[int] = set()

        for recognizer_class in RECOGNIZER_PIPELINE:
            if check_abort and check_abort():
                break
            recognizer = recognizer_class()
            recognizer.config = self.config
            recognizer.part_process = part_process
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

        return result
