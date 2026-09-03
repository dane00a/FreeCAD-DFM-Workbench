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
from .pocket_recognizer import PocketRecognizer
from .slot_recognizer import SlotRecognizer
from .undercut_recognizer import UndercutRecognizer

#: Recognizers in pipeline order.
RECOGNIZER_PIPELINE: list[type[FeatureRecognizer]] = [
    HoleRecognizer,
    PocketRecognizer,
    SlotRecognizer,
    # Last of the cavity group: it is told which faces the others claimed,
    # so a bore is never reported as unreachable from the side.
    UndercutRecognizer,
]

__all__ = [
    "FeatureRecognizer",
    "HoleRecognizer",
    "PocketRecognizer",
    "SlotRecognizer",
    "UndercutRecognizer",
    "RECOGNIZER_PIPELINE",
]
