# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Feature recognizers, and the fixed order they run in.

The order is load-bearing. Later recognizers are told which faces earlier ones
claimed, so a groove does not re-recognize the bore it sits in and a pattern
can group holes that already exist.

Broadly the order runs from the most specific reading of a face to the most
general. Holes come first because a bore is unambiguous and everything else
benefits from knowing where they are. Cavities follow, then the protrusions
that are defined by what surrounds them, then the whole-part readings --
draft, turned profile -- that need the rest already settled. Marking runs
almost last on purpose: an engraved character is a slot and a boss and an
undercut all at once by any local test, so it has to be able to overrule
claims the earlier passes made on its strokes.
"""

from .base import FeatureRecognizer
from .bend_recognizer import BendRecognizer
from .blend_recognizer import BlendRecognizer
from .boss_recognizer import BossRecognizer
from .channel_recognizer import ChannelRecognizer
from .draft_recognizer import DraftRecognizer
from .external_thread_recognizer import ExternalThreadRecognizer
from .groove_recognizer import GrooveRecognizer
from .hole_recognizer import HoleRecognizer
from .marking_recognizer import MarkingRecognizer
from .pattern_recognizer import PatternRecognizer
from .pocket_recognizer import PocketRecognizer
from .rib_recognizer import RibRecognizer
from .sheet_formed_recognizer import SheetFormedRecognizer
from .sheet_outline_recognizer import SheetOutlineRecognizer
from .slit_recognizer import SlitRecognizer
from .slot_recognizer import SlotRecognizer
from .spherical_pocket_recognizer import SphericalPocketRecognizer
from .step_recognizer import StepRecognizer
from .through_cavity_recognizer import ThroughCavityRecognizer
from .turned_profile_recognizer import TurnedProfileRecognizer
from .undercut_recognizer import UndercutRecognizer

#: Recognizers in pipeline order.
RECOGNIZER_PIPELINE: list[type[FeatureRecognizer]] = [
    HoleRecognizer,
    # Before the grooves, so a DIN 76 relief next to a thread is read as a
    # relief rather than as an unexplained groove.
    ExternalThreadRecognizer,
    PocketRecognizer,
    SlotRecognizer,
    SlitRecognizer,
    ThroughCavityRecognizer,
    BlendRecognizer,
    StepRecognizer,
    BossRecognizer,
    RibRecognizer,
    ChannelRecognizer,
    SphericalPocketRecognizer,
    # After the holes, so a bore is never re-read as a groove, and after the
    # cavities so its shoulders are not already claimed.
    GrooveRecognizer,
    # Told which faces the others claimed, so a bore is never reported as
    # unreachable from the side.
    UndercutRecognizer,
    DraftRecognizer,
    TurnedProfileRecognizer,
    # Late, so it can overrule the per-stroke claims the cavity and
    # protrusion passes made on engraved characters.
    MarkingRecognizer,
    # Last: it groups features the others have already produced.
    PatternRecognizer,
]

#: The formed-sheet group, which runs after the machining pipeline has been
#: resolved rather than as part of it.
#:
#: A fold is a BEND and its two cylinders are each also a FILLET, and both
#: readings are wanted -- the bend rules read one, the corner and freeform
#: rules read the other. Put through the resolver together, the bend contains
#: the fillets and the resolver drops them, which is the right answer to the
#: question it was asked and the wrong answer here.
#:
#: Running after also means a part the classifier did not call sheet metal
#: cannot pick up a bend, so every milled and turned result is untouched by
#: this pass. Bends come first within the group: they claim the fold
#: cylinders before the hood pass can mistake one for a swept crest, and
#: before the outline pass can read a hem as a connecting strip.
SHEET_PIPELINE: list[type[FeatureRecognizer]] = [
    BendRecognizer,
    SheetOutlineRecognizer,
    SheetFormedRecognizer,
]

__all__ = [
    "SHEET_PIPELINE",
    "BendRecognizer",
    "BlendRecognizer",
    "BossRecognizer",
    "ChannelRecognizer",
    "DraftRecognizer",
    "ExternalThreadRecognizer",
    "FeatureRecognizer",
    "GrooveRecognizer",
    "HoleRecognizer",
    "MarkingRecognizer",
    "PatternRecognizer",
    "PocketRecognizer",
    "RECOGNIZER_PIPELINE",
    "RibRecognizer",
    "SheetFormedRecognizer",
    "SheetOutlineRecognizer",
    "SlitRecognizer",
    "SlotRecognizer",
    "SphericalPocketRecognizer",
    "StepRecognizer",
    "ThroughCavityRecognizer",
    "TurnedProfileRecognizer",
    "UndercutRecognizer",
]
