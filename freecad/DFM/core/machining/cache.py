# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""The per-shape machining cache.

Machining recognition is a shape-level computation, the same as the feature
recognition in ``core.recognition``, and is cached the same way: by the
shape's geometric fingerprint (stable across processes and sessions, unlike
TopoDS hash codes), the configuration, the inputs the part states about
itself, and the engine version. A changed shape, changed thresholds, a newly
confirmed thread or a new engine all miss.

The fingerprint is ``core.recognition``'s own, so the two engines agree on
when two shapes are the same shape.
"""

from __future__ import annotations

import copy
import hashlib
import threading
from collections import OrderedDict
from typing import Any

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.TopoDS import TopoDS, TopoDS_Shape

from ..recognition.cache import fingerprint
from ._version import ENGINE_VERSION


def _digest(value: Any) -> str:
    return hashlib.sha1(repr(value).encode()).hexdigest()[:16]


def edge_fingerprint(shape: TopoDS_Shape) -> str:
    """SHA-1 over each edge's curve type, length and midpoint, in edge order.

    The face fingerprint cannot tell a cylinder from the same cylinder with
    its seam turned: the faces have the same area and centroid either way.
    The machining context names edges as well as faces, so a part that hit
    the cache on faces alone would be handed another part's edge numbers.
    """
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, edges)
    h = hashlib.sha1()
    for i in range(1, edges.Extent() + 1):
        edge = TopoDS.Edge_s(edges.FindKey(i))
        props = GProp_GProps()
        BRepGProp.LinearProperties_s(edge, props)
        c = props.CentreOfMass()
        try:
            kind = int(BRepAdaptor_Curve(edge).GetType())
        except Exception:  # a degenerate edge has no curve to adapt
            kind = -1
        h.update(f"|{kind}:{props.Mass():.4f}:{c.X():.4f}:{c.Y():.4f}:{c.Z():.4f}".encode())
    return h.hexdigest()


class MachiningCache:
    """A small LRU of machining contexts keyed by fingerprint + inputs."""

    def __init__(self, maxsize: int = 4):
        self.maxsize = maxsize
        self._data: "OrderedDict[tuple, object]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(shape: TopoDS_Shape, config: Any, inputs: Any = None) -> tuple:
        """Everything the context depends on besides the code itself.

        ``config`` is the resolved :class:`~.config.MachiningConfig`, whose
        dataclass repr spells out every threshold and tool. ``inputs`` is
        whatever the part states about itself -- thread declarations and
        confirmations, PMI -- reduced to something whose repr is stable.
        """
        return (
            fingerprint(shape),
            edge_fingerprint(shape),
            _digest(config),
            _digest(inputs),
            ENGINE_VERSION,
        )

    def lookup(self, key: tuple) -> Any:
        """A copy of the context cached under *key*, or None.

        Shallow: the graph and the features are shared, and nothing
        downstream changes them. The context's own attributes are not, so a
        caller that swaps one (a test supplying a different tool library)
        changes its copy and not the next caller's.
        """
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self.hits += 1
                return copy.copy(self._data[key])
            self.misses += 1
            return None

    def store(self, key: tuple, value: Any) -> None:
        """Keep *value* under *key*. Only a complete pass is stored: an
        aborted one would be read back by the next run as the whole part."""
        with self._lock:
            self._data[key] = copy.copy(value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


#: The one machining cache of the workbench, as FEATURE_CACHE is for
#: feature recognition. Four parts: a machining context holds the face graph
#: and every feature of a part, which is far heavier than a recognition result.
MACHINING_CACHE = MachiningCache(maxsize=4)
