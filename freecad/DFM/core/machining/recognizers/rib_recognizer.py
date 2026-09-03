# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Recognizes stiffening ribs.

Not yet implemented. The recognizer occupies its place in the pipeline so the
order is settled and the rules that read its output can be written against a
type that already exists.
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..aag import AttributedAdjacencyGraph
from ..features import FeatureInstance
from .base import FeatureRecognizer


class RibRecognizer(FeatureRecognizer):
    """Recognizes stiffening ribs."""

    prefix = "ri"

    @property
    def name(self) -> str:
        return "Rib Recognizer"

    def recognize(
        self,
        graph: AttributedAdjacencyGraph,
        shape=None,
        claimed: Optional[set[int]] = None,
        prior: Optional[Sequence[FeatureInstance]] = None,
    ) -> list[FeatureInstance]:
        return []
