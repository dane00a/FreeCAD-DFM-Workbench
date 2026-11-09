# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Tests for the half of the corpus that arrives as a file.

Two of the three things worth checking here can be checked anywhere, and
the third cannot be checked at all on most machines. The STEP files are
gitignored, so a fresh clone has none of them, and a test that assumed
otherwise would fail for everyone who had not copied twelve parts in by
hand. So the rules about merging and about absence are tested on dictionaries
and on a directory that is not there -- neither needs a single byte of
geometry -- and only the one test that genuinely has to open a part is
skipped when there is no part to open.

Which leaves absence itself as the case most worth getting right, because it
is the common one. The corpus has to come up without these, offer the
fixtures it does have, and say plainly that the rest were not checked.
"""

import os
import tempfile
import unittest

from freecad.DFM.core.baselines import corpus, step_fixtures


def shape_counts(shape):
    """Faces and solids in a shape, without importing half of OpenCascade
    into every test that wants to know whether a read worked."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    counts = []
    for kind in (TopAbs_FACE, TopAbs_SOLID):
        found = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, kind, found)
        counts.append(found.Extent())
    return tuple(counts)


def a_step_file_is_installed(name="nist_ctc_01"):
    return os.path.isfile(os.path.join(step_fixtures.STEP_DIR, name + ".step"))


# =============================================================================


class TestReadingAStepFixture(unittest.TestCase):
    """That an imported part comes back as a part, not as a null shape."""

    @unittest.skipUnless(
        a_step_file_is_installed(), "nist_ctc_01.step is not installed"
    )
    def test_a_fixture_builds_one_solid(self):
        shape = step_fixtures.build("nist_ctc_01")
        self.assertFalse(shape.IsNull())
        faces, solids = shape_counts(shape)
        # One solid is the contract every rule in the machining set assumes;
        # a STEP that transferred as a compound of shells would analyse to
        # nothing at all and look like a part with no findings.
        self.assertEqual(solids, 1)
        self.assertGreater(faces, 0)

    @unittest.skipUnless(
        a_step_file_is_installed(), "nist_ctc_01.step is not installed"
    )
    def test_the_second_build_does_not_reparse(self):
        # Six megabytes of ASCII STEP takes long enough that re-reading it
        # once per rule run is the difference between a corpus run and a
        # coffee break.
        first = step_fixtures.build("nist_ctc_01")
        self.assertIs(step_fixtures.build("nist_ctc_01"), first)

    @unittest.skipUnless(
        a_step_file_is_installed(), "nist_ctc_01.step is not installed"
    )
    def test_the_fixture_reaches_the_merged_registry(self):
        registry = corpus.load_registry()
        self.assertIn("nist_ctc_01", registry)
        self.assertFalse(registry["nist_ctc_01"]().IsNull())

    def test_a_file_that_is_not_step_is_an_error_not_a_null_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "not_really.step")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("this is not a STEP file\n")
            with self.assertRaises(step_fixtures.StepFixtureError):
                step_fixtures.read_step(path)

    def test_a_name_with_no_file_is_an_error(self):
        with self.assertRaises(step_fixtures.StepFixtureError):
            step_fixtures.build("no_such_nist_part")


# =============================================================================


class TestTheTwoSourcesCannotShadowEachOther(unittest.TestCase):
    """A name offered by both a recipe and a file is a bug, not a preference.

    Tested on plain dictionaries because the rule is about names, and making
    it depend on twelve installed STEP files would mean the rule went
    untested on exactly the machines most likely to add a clashing recipe.
    """

    def test_a_shared_name_raises(self):
        with self.assertRaises(corpus.CorpusCollision) as caught:
            corpus.merge_sources(
                {"deep_hole": lambda: None, "nist_ctc_01": lambda: None},
                {"nist_ctc_01": lambda: None},
            )
        self.assertIn("nist_ctc_01", str(caught.exception))
        self.assertNotIn("deep_hole", str(caught.exception))

    def test_every_clash_is_named_at_once(self):
        # Reporting the first and stopping would mean renaming one fixture,
        # re-running, and finding the next.
        with self.assertRaises(corpus.CorpusCollision) as caught:
            corpus.merge_sources(
                {"a": lambda: None, "b": lambda: None},
                {"a": lambda: None, "b": lambda: None},
            )
        self.assertIn("a", str(caught.exception))
        self.assertIn("b", str(caught.exception))

    def test_distinct_names_merge(self):
        merged = corpus.merge_sources(
            {"deep_hole": lambda: 1}, {"nist_ctc_01": lambda: 2}
        )
        self.assertEqual(sorted(merged), ["deep_hole", "nist_ctc_01"])
        self.assertEqual(merged["deep_hole"](), 1)
        self.assertEqual(merged["nist_ctc_01"](), 2)


# =============================================================================


class TestAbsenceOfTheStepFiles(unittest.TestCase):
    """The fresh-clone case: recipes present, imported parts nowhere."""

    def setUp(self):
        self.original = step_fixtures.STEP_DIR
        self.temporary = tempfile.TemporaryDirectory()
        # A path inside a real directory but not itself created, so that
        # this is absence rather than an empty directory -- the two take
        # different branches and both have to come back quiet.
        step_fixtures.STEP_DIR = os.path.join(self.temporary.name, "step_pmi")
        step_fixtures.clear_cache()

    def tearDown(self):
        step_fixtures.STEP_DIR = self.original
        step_fixtures.clear_cache()
        self.temporary.cleanup()

    def test_nothing_is_offered_and_nothing_raises(self):
        self.assertFalse(step_fixtures.available())
        self.assertEqual(step_fixtures.load_registry(), {})
        self.assertEqual(step_fixtures.fixture_names(), [])

    def test_an_empty_directory_reads_the_same_as_a_missing_one(self):
        os.makedirs(step_fixtures.STEP_DIR)
        self.assertFalse(step_fixtures.available())
        self.assertEqual(step_fixtures.load_registry(), {})

    def test_the_absent_fixtures_are_named(self):
        # The names are the only committed trace that these parts exist.
        # Without them a run that checks nothing cannot tell anyone so.
        self.assertEqual(
            step_fixtures.missing_fixtures(), list(step_fixtures.EXPECTED_FIXTURES)
        )
        self.assertEqual(len(step_fixtures.EXPECTED_FIXTURES), 12)

    def test_the_corpus_still_offers_the_recipes(self):
        registry = corpus.load_registry()
        if not corpus.builders_available():
            self.skipTest("no fixture recipes installed either")
        self.assertTrue(corpus.corpus_available())
        self.assertTrue(registry)
        for name in step_fixtures.EXPECTED_FIXTURES:
            self.assertNotIn(name, registry)

    def test_building_an_absent_fixture_says_which_one(self):
        with self.assertRaises(step_fixtures.StepFixtureError) as caught:
            step_fixtures.build("nist_ftc_09")
        self.assertIn("nist_ftc_09", str(caught.exception))


# =============================================================================


class TestTheInstalledSet(unittest.TestCase):
    """That an installation which has the files has all of them."""

    @unittest.skipUnless(
        step_fixtures.available(), "no imported STEP fixtures installed"
    )
    def test_a_partial_install_is_reported_rather_than_assumed(self):
        # Not an assertion that nothing is missing -- someone may well have
        # copied in only the part they were debugging. It is an assertion
        # that whatever is missing is named, so the corpus run can skip it
        # out loud instead of silently.
        present = set(step_fixtures.fixture_names())
        missing = set(step_fixtures.missing_fixtures())
        self.assertFalse(present & missing)
        self.assertEqual(
            present | missing, set(step_fixtures.EXPECTED_FIXTURES) | present
        )


if __name__ == "__main__":
    unittest.main()
