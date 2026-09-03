# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Feature recognizers, and the fixed order they run in.

The order is load-bearing. Later recognizers are told which faces earlier
ones claimed, so a groove does not re-recognize the bore it sits in and a
pattern can group holes that already exist.
"""

from .base import FeatureRecognizer
from .hole_recognizer import HoleRecognizer

#: Recognizers in pipeline order.
RECOGNIZER_PIPELINE: list[type[FeatureRecognizer]] = [
    HoleRecognizer,
]

__all__ = ["FeatureRecognizer", "HoleRecognizer", "RECOGNIZER_PIPELINE"]
