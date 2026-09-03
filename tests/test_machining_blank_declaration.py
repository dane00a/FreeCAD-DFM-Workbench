# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""A part's own statement about the stock it was cut from."""

import unittest

from freecad.DFM.core.machining.blank_declaration import (
    BLANK_CHOICES,
    BLANK_PROPERTY,
    apply_declaration,
    declare_blank,
    declared_blank,
)


class FakeObject:
    """Enough of a document object to hold a property."""

    def __init__(self):
        self._properties: list[str] = []

    def addProperty(self, kind, name, group, doc):
        self._properties.append(name)
        setattr(self, name, "")
        return self


class RefusesProperties(FakeObject):
    def addProperty(self, kind, name, group, doc):
        raise RuntimeError("read-only object")


class BlankDeclarationTests(unittest.TestCase):
    def test_undeclared_object_says_nothing(self):
        self.assertIsNone(declared_blank(FakeObject()))

    def test_missing_object_says_nothing(self):
        self.assertIsNone(declared_blank(None))

    def test_declaring_adds_the_property_once(self):
        obj = FakeObject()
        self.assertTrue(declare_blank(obj, "billet"))
        self.assertEqual(declared_blank(obj), "billet")
        self.assertTrue(declare_blank(obj, "as_cast"))
        self.assertEqual(obj._properties.count(BLANK_PROPERTY), 1)
        self.assertEqual(declared_blank(obj), "as_cast")

    def test_the_enumeration_offers_an_undeclared_state_first(self):
        self.assertEqual(BLANK_CHOICES[0], "")
        self.assertIn("billet", BLANK_CHOICES)

    def test_retracting_leaves_the_property_in_place(self):
        obj = FakeObject()
        declare_blank(obj, "billet")
        self.assertTrue(declare_blank(obj, ""))
        self.assertIsNone(declared_blank(obj))
        self.assertTrue(hasattr(obj, BLANK_PROPERTY))

    def test_an_unknown_form_is_refused_rather_than_stored(self):
        obj = FakeObject()
        self.assertFalse(declare_blank(obj, "unobtanium"))
        self.assertIsNone(declared_blank(obj))

    def test_a_form_from_a_later_version_reads_as_undeclared(self):
        """A document written by a newer release must not change the rules."""
        obj = FakeObject()
        declare_blank(obj, "billet")
        setattr(obj, BLANK_PROPERTY, "sintered_preform")
        self.assertIsNone(declared_blank(obj))

    def test_an_object_that_refuses_properties_is_not_an_error(self):
        obj = RefusesProperties()
        self.assertFalse(declare_blank(obj, "billet"))

    def test_the_part_outranks_the_shop(self):
        obj = FakeObject()
        declare_blank(obj, "billet")
        prefs = apply_declaration({"MachiningBlankForm": "as_cast"}, obj)
        self.assertEqual(prefs["MachiningBlankForm"], "billet")

    def test_an_undeclared_part_leaves_the_shop_setting_alone(self):
        prefs = {"MachiningBlankForm": "as_cast", "Other": 1}
        self.assertEqual(apply_declaration(prefs, FakeObject()), prefs)
        self.assertEqual(apply_declaration(prefs, None), prefs)

    def test_applying_does_not_mutate_the_caller_s_preferences(self):
        original = {"MachiningBlankForm": "as_cast"}
        obj = FakeObject()
        declare_blank(obj, "billet")
        apply_declaration(original, obj)
        self.assertEqual(original["MachiningBlankForm"], "as_cast")


if __name__ == "__main__":
    unittest.main()
