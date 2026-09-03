# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes cavities open at both ends.

Not yet implemented. The recognizer occupies its place in the pipeline so the
order is settled and the rules that read its output can be written against a
type that already exists.
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..aag import AttributedAdjacencyGraph
from ..features import FeatureInstance
from .base import FeatureRecognizer


class ThroughCavityRecognizer(FeatureRecognizer):
    """Recognizes cavities open at both ends."""

    prefix = "tc"

    @property
    def name(self) -> str:
        return "Through Cavity Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        return []
