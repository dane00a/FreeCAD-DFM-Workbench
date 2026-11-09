# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Product and manufacturing information read from STEP files.

:func:`read_step_pmi` is the whole interface: a STEP path and the shape the
analysis runs on, in; an :class:`~..annotations.AnnotationSet` keyed by that
shape's face ids, out. A caller hands the set to the machining analyzer as
its ``annotations`` argument and every GD&T rule reads it from there.

The OCP-dependent modules are imported lazily so that the package can be
named from code that only wants the vocabulary.
"""

from __future__ import annotations


def read_step_pmi(path, analysis_shape):
    from .xde import read_step_pmi as _read

    return _read(path, analysis_shape)


__all__ = ["read_step_pmi"]
