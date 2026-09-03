# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Machining DFM: adjacency graph, feature recognition and machining rules."""

from .aag import AagEdge, AagNode, AttributedAdjacencyGraph, Concavity, SurfaceType
from .aag_builder import AagBuilder

__all__ = [
    "AagBuilder",
    "AagEdge",
    "AagNode",
    "AttributedAdjacencyGraph",
    "Concavity",
    "SurfaceType",
]
