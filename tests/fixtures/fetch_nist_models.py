# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Dane Andrews <dane.andrews99@gmail.com>
# SPDX-FileNotice: Part of the DFM addon.

"""Fetch the imported STEP fixtures, which the repository does not carry.

Twelve of the corpus's parts are real CAD rather than recipes: NIST's MBE PMI
test cases, which cannot be built from boxes and cylinders the way the other
213 are. Together they are twenty-four megabytes, against a repository whose
entire tracked history is under three. Committing them would make the addon
an order of magnitude larger for every person who installs it through the
Addon Manager and never runs a test, so ``.gitignore`` excludes ``*.step`` and
this script provisions them instead.

What is committed is this file: the names, sizes and hashes. Measurements, not
geometry -- the same division ``geometry_oracle.json`` draws, and nothing here
can reconstruct a shape.

    python tests/fixtures/fetch_nist_models.py           # download and install
    python tests/fixtures/fetch_nist_models.py --check   # verify what is there
    python tests/fixtures/fetch_nist_models.py --list    # what is missing

Without these files the corpus still runs. ``check_dfm_baselines.py`` reports
the imported fixtures as skipped and checks the other 213, which is the right
behaviour for a fresh clone: loud about what it did not do, and green about
what it did.

``nist_ctc_02_logo`` is deliberately absent from the archive, by decision
rather than by oversight. NIST dedicates these models to the public domain
under 17 U.S.C. 105 and asks only for acknowledgement, with one exception --
"the use of the NIST logo is not allowed" -- and that fixture is the variant
carrying an embossed NIST logo on its side face, twenty-seven faces the plain
``nist_ctc_02`` does not have. The dedication covers the geometry; whether it
covers redistributing the mark is a question the README does not answer, and
an unanswered licence question is not a thing to settle by publishing and
seeing who objects.

So it is withheld from the release and the corpus carries on without it. A
developer who has the file locally loses nothing -- the fixture registers and
its baseline is checked like any other -- and anyone who does not gets told
which file is missing and why, rather than a silent gap. Revisiting the
decision means adding one entry to ``MANIFEST``, rebuilding the archive with
twelve files, and updating ``ARCHIVE_SHA256``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import tarfile
import urllib.error
import urllib.request
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
STEP_DIR = os.path.join(_HERE, "step_pmi")

#: Where the archive lives. A release asset rather than the repository, so the
#: bytes are versioned and addressable without being cloned.
ARCHIVE_URL = (
    "https://github.com/dane00a/FreeCAD-DFM-Workbench/releases/download/"
    "nist-fixtures-v1/nist-step-pmi-v1.tar.gz"
)
ARCHIVE_SHA256 = "0e44276158c866ef5a4804af2a13a2dd1eefaf909f8666f53b5f14902ee616bc"

#: name -> (size in bytes, sha256). Checked per file after extraction rather
#: than trusting the archive hash alone: an archive that matches can still be
#: unpacked into a directory that already holds a half-written file from an
#: interrupted run, and a truncated STEP reads as a valid smaller solid rather
#: than as an error.
MANIFEST: dict[str, tuple[int, str]] = {
    "nist_ctc_01.step": (
        396445,
        "85a5752da05f53c456ca3a9e038c90358e1d5a3141d1f0d6e5f0970f2356e821",
    ),
    "nist_ctc_02.step": (
        1981342,
        "99a0a2079ddeb64d05c2432cbe931fa17c89da60d8b0bcb0b11bd0ed94fa2e68",
    ),
    "nist_ctc_03.step": (
        673846,
        "196b665776e759282f80fc8fb27d7bceb995df77cf78b7ce48347535a4d6cb5f",
    ),
    "nist_ctc_04.step": (
        1287291,
        "20b43b54ce25d4ed17cff794084c406e831c687f5b62471d3371eef33669e355",
    ),
    "nist_ctc_05.step": (
        878030,
        "59bbc09a34621c03106e4c1b2a5bc909fdb67463117c16c4965ee7a6fe5c1521",
    ),
    "nist_ftc_06.step": (
        1971192,
        "b87afb7f25adc0d44d12e182209d50f0b085b533c0742fb8ffd58f4c87403447",
    ),
    "nist_ftc_07.step": (
        1478375,
        "aa8115c12e12a99cc439477cbfd222916f9ec629fbb3c9002b18e0c264c9a892",
    ),
    "nist_ftc_08.step": (
        4722264,
        "7b9283b4288d152fd181d76c39285d7c79fbba34fda1e14a41ac4a983f6d68b3",
    ),
    "nist_ftc_09.step": (
        6109836,
        "f1215fe15a78085a9fa78dd81714caf774b59071be92580b04ebfdc19f52a1bd",
    ),
    "nist_ftc_10.step": (
        1879881,
        "9d7711f48e81831c5d1838fdac5c11c0de1aa9b61f3c1d67930a7638b3b25e69",
    ),
    "nist_ftc_11.step": (
        1946454,
        "20a92edf514ae0989d556f9c7b9f065aed741cfbb361b7fe4cb7938a1eb5c232",
    ),
}

#: Present in the corpus, absent from the archive. See the module docstring.
WITHHELD: dict[str, tuple[int, str]] = {
    "nist_ctc_02_logo.step": (
        1154600,
        "e6df6d0ed68fd8a91bd3f8360c7047f2af3d61068b89050d84423fe8ddb93bcf",
    ),
}


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def status() -> tuple[list[str], list[str], list[str]]:
    """What is installed and sound, what is missing, what is wrong.

    Wrong is kept apart from missing because they call for different
    responses. A missing file is a fresh clone and this script fixes it; a
    file whose hash does not match is either a truncated download or a part
    somebody edited, and re-downloading over it without saying so would erase
    the evidence of whichever it was.
    """
    ok: list[str] = []
    missing: list[str] = []
    corrupt: list[str] = []
    for name, (size, digest) in {**MANIFEST, **WITHHELD}.items():
        path = os.path.join(STEP_DIR, name)
        if not os.path.exists(path):
            missing.append(name)
        elif os.path.getsize(path) != size or _sha256(path) != digest:
            corrupt.append(name)
        else:
            ok.append(name)
    return sorted(ok), sorted(missing), sorted(corrupt)


def download(url: str = ARCHIVE_URL) -> bytes:
    try:
        with urllib.request.urlopen(url) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SystemExit(
                f"{url}\n  404. The release asset is not published yet, or the "
                f"tag moved. Nothing was written."
            ) from exc
        raise SystemExit(f"{url}\n  HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"{url}\n  {exc.reason}") from exc


def install(data: bytes) -> list[str]:
    """Verify the archive, then unpack it a member at a time.

    Members are checked against the manifest by name before anything is
    written, so an archive that grew a path like ``../../something`` cannot
    write outside ``step_pmi`` -- tarfile will happily do that on request and
    this is the cheapest place to refuse.
    """
    digest = hashlib.sha256(data).hexdigest()
    if digest != ARCHIVE_SHA256:
        raise SystemExit(
            f"archive hash mismatch\n  expected {ARCHIVE_SHA256}\n"
            f"  got      {digest}\nNothing was written."
        )

    os.makedirs(STEP_DIR, exist_ok=True)
    written: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            name = os.path.basename(member.name)
            if not member.isfile() or name not in MANIFEST or name != member.name:
                print(f"  ignored unexpected member: {member.name}")
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            target = os.path.join(STEP_DIR, name)
            with open(target, "wb") as handle:
                handle.write(source.read())
            written.append(name)
    return sorted(written)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the installed files against the manifest and stop",
    )
    parser.add_argument(
        "--list", action="store_true", help="say what is installed and what is not"
    )
    parser.add_argument("--url", default=ARCHIVE_URL, help="override the archive URL")
    args = parser.parse_args(argv)

    ok, missing, corrupt = status()

    if args.list or args.check:
        for name in ok:
            print(f"  ok      {name}")
        for name in missing:
            note = " (withheld from the archive)" if name in WITHHELD else ""
            print(f"  missing {name}{note}")
        for name in corrupt:
            print(f"  WRONG   {name}: size or hash does not match the manifest")
        installable = [n for n in missing if n in MANIFEST]
        print(
            f"\n{len(ok)} present, {len(missing)} missing "
            f"({len(installable)} of them fetchable), {len(corrupt)} wrong"
        )
        if corrupt:
            return 2
        return 1 if installable else 0

    if not [n for n in missing if n in MANIFEST] and not corrupt:
        print(f"all {len(MANIFEST)} fetchable fixtures are installed and match")
        return 0

    if corrupt:
        print("refusing to overwrite files that do not match the manifest:")
        for name in corrupt:
            print(f"  {name}")
        print("Move or delete them first, then run again.")
        return 2

    print(f"fetching {args.url}")
    written = install(download(args.url))
    _, still_missing, now_corrupt = status()
    if now_corrupt:
        print("extracted files do not match the manifest:")
        for name in now_corrupt:
            print(f"  {name}")
        return 2
    print(f"installed {len(written)} file(s) into {STEP_DIR}")
    withheld = [n for n in still_missing if n in WITHHELD]
    if withheld:
        print(
            f"still missing (withheld from the archive, see the module "
            f"docstring): {', '.join(withheld)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
