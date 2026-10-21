"""Pinned interpretations of patent tables used by the Lens DB bridge.

These records distinguish a table declaration from an optical modelling
assumption.  A patent's *effective diameter* does not establish a measured
mechanical clear aperture or a calibrated transmission model.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SCHEMA = "camerae2e_lens_patent_source_review_v1"

# The archived source is the US patent HTML retained by Lens_Patent_DB.  The
# reviewed CSV has the same numbered 28th row and terminal image-plane caption.
# This one reviewed case is deliberately exact-keyed; other revisions receive
# no inferred interpretation.
_P0014_REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
_P0014_CSV_SHA256 = "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd"
_P0014_SOURCE_SHA256 = "648c9cd8828095f60526d6c7caede8c7f5349842fbf31cb48cff98d29ab46f27"


def verify_archived_p0014_patent(path: str | Path) -> dict[str, Any]:
    """Recheck the exact archived primary text behind the source review.

    This optional audit is separate from installed inference: the archive is
    owned by Lens_Patent_DB and is not needed to run a pinned package.
    """
    source = Path(path)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != _P0014_SOURCE_SHA256:
        raise ValueError("Archived patent bytes differ from the reviewed source")
    text = data.decode("utf-8")
    start = text.find("Numerical Emodiment 1 Unit mm Surface data")
    if start < 0:
        raise ValueError("Archived patent lacks the reviewed first surface table")
    end = text.find("Aspherical surface data", start)
    if end < 0:
        raise ValueError("Archived patent first surface table has no bounded end")
    table = " ".join(text[start:end].split())
    if not (
        re.search(r"Surface Effective number r d nd νd diameter", table)
        and re.search(r"28\s+∞\s+0\.50\s+50\.00\s+Image plane\s+∞", table)
    ):
        raise ValueError("Archived patent does not declare the reviewed diameter and plane")
    return {
        "schema": SCHEMA,
        "status": "source_text_verified",
        "archived_patent_html_sha256": digest,
        "source_url": "https://patents.google.com/patent/US10310255B2/en",
        "table": "Numerical Embodiment 1, Surface data",
        "effective_diameter_declared": True,
        "separate_numbered_28_and_image_plane": True,
        "physical_clear_aperture_verified": False,
    }


def reviewed_source_semantics(
    revision_id: str, csv_sha256: str, rows: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return a reviewed table interpretation only for matching exact bytes.

    The caller has already verified the CSV hash against the DB revision.  We
    still cross-check the terminal numeric row to avoid attaching an unrelated
    patent-text review to a differently parsed prescription.
    """
    if (revision_id, csv_sha256) != (_P0014_REVISION, _P0014_CSV_SHA256):
        return None
    if len(rows) != 28:
        raise ValueError("Reviewed patent source does not match the 28-row CSV")
    rear = rows[-1]
    if not (
        rear["surface_order"] == 28
        and rear["surface_kind"] == "image"
        and rear["radius_mm"] == 0.0
        and math.isclose(rear["thickness_mm"], 0.5, abs_tol=1e-12)
        and math.isclose(rear["effective_aperture_mm"], 50.0, abs_tol=1e-12)
    ):
        raise ValueError("Reviewed patent terminal row differs from the pinned CSV")
    return {
        "schema": SCHEMA,
        "scope": "one_exact_patent_table_and_reviewed_csv",
        "publication_number": "US10310255B2",
        "source_url": "https://patents.google.com/patent/US10310255B2/en",
        "archived_patent_html_sha256": _P0014_SOURCE_SHA256,
        "table": "Numerical Embodiment 1, Surface data",
        "reviewed_csv_sha256": _P0014_CSV_SHA256,
        "effective_aperture_field_meaning": "patent_declares_effective_diameter_mm",
        "terminal_numbered_row": {
            "surface_order": 28,
            "radius": "plane",
            "thickness_to_image_plane_mm": 0.5,
            "effective_diameter_mm": 50.0,
            "separate_image_plane_caption": True,
            "optical_role": "last_plane_interface_before_image_plane",
        },
        "physical_clear_aperture_verified": False,
        "absolute_transmission_verified": False,
        "interpretation": (
            "The numbered 28th row is retained. A distinct unnumbered image-plane "
            "caption follows it; the source column heading is Effective diameter. "
            "Using that diameter as a hard clear-aperture clip is a modelling proxy."
        ),
    }
