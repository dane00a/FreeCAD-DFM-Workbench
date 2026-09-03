# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""What the stock looked like before anyone cut it, stated per part.

The blank is the one thing about a machined part that the geometry cannot
recover. A casting that has been machined all over and a billet part are the
same solid: the flash is gone, the draft is gone, and what is left is the
finished shape either way. The same is true of bar stock -- a part sawn from
a drawn profile and the identical part hogged from a block are one model.

So it has to be declared. It already can be, once, for the whole shop, on the
Machining preferences page -- and that is right for a shop that buys one kind
of stock. It is wrong for the shop that does both, because the declaration is
a fact about the part in front of you, not about the building.

This keeps it with the part. The declaration is an ordinary FreeCAD property
in a DFM group, so it saves with the document, travels with the file, appears
in the property editor next to everything else about the object, and undoes
like anything else. An object that has never been declared says nothing, and
the shop-wide preference continues to apply.
"""

from __future__ import annotations

from typing import Optional

from .config import BLANK_FORMS


#: The property the declaration lives in. Named for the addon so it cannot
#: collide with a property the object's own workbench owns.
BLANK_PROPERTY = "DfmBlankForm"

#: The group it appears under in the property editor.
BLANK_GROUP = "DFM"

#: What the enumeration offers, in the order it is offered. The empty first
#: entry is what an object means when nobody has said anything about it: not
#: "unknown material" but "no declaration here, use the shop's".
BLANK_CHOICES = ("",) + tuple(form for form in BLANK_FORMS if form)

BLANK_TOOLTIP = (
    "What this part's stock was before machining.\n\n"
    "No analysis can work this out -- a machined casting and a machined\n"
    "billet are the same solid once the flash is off. Leave it empty to\n"
    "use the shop-wide setting from Preferences -> DFM -> Machining."
)


def declared_blank(obj) -> Optional[str]:
    """The blank this object was declared to come from, if any.

    Returns None rather than the empty string when nothing was declared, so
    a caller can tell "not declared" from a declaration it does not
    recognize -- a document written by a later version, say.
    """
    if obj is None:
        return None
    value = getattr(obj, BLANK_PROPERTY, None)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value not in BLANK_FORMS:
        return None
    return value


def declare_blank(obj, form: str) -> bool:
    """Record on an object which blank it was cut from.

    Adds the property the first time, so an object only carries it once
    somebody has had something to say. Passing the empty string retracts the
    declaration without removing the property, which keeps the choice
    visible in the property editor -- an object that has been thought about
    and left undeclared should not look like one nobody has opened.
    """
    if obj is None:
        return False
    if form and form not in BLANK_FORMS:
        return False

    if not hasattr(obj, BLANK_PROPERTY):
        try:
            obj.addProperty(
                "App::PropertyEnumeration",
                BLANK_PROPERTY,
                BLANK_GROUP,
                BLANK_TOOLTIP,
            )
        except Exception:
            # An object that refuses properties -- a link to another
            # document, a read-only feature -- is not an error worth
            # stopping an analysis for. The shop-wide setting still applies.
            return False
        try:
            setattr(obj, BLANK_PROPERTY, list(BLANK_CHOICES))
        except Exception:
            return False

    try:
        setattr(obj, BLANK_PROPERTY, form or "")
    except Exception:
        return False
    return True


def apply_declaration(prefs: dict, obj) -> dict:
    """Let a part's own declaration outrank the shop-wide preference.

    Returns the preferences to analyse with. The shop's setting is the
    default for every part in the building; the part's own is a statement
    about this one, and the more specific statement wins.
    """
    form = declared_blank(obj)
    if form is None:
        return prefs
    updated = dict(prefs)
    updated["MachiningBlankForm"] = form
    return updated
