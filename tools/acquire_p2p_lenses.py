#!/usr/bin/env python3
"""Acquire a cumulative, balanced selection from the public Photons to Photos lens index.

Only URLs exposed by OpticalBenchHub's modeldata/click handler are fetched.
No unpublished patent/example enumeration, JavaScript execution, or access bypass.
The source's public optical prescriptions remain subject to its own rights notice.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import re
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://www.photonstophotos.net/GeneralTopics/Lenses/OpticalBench/"
HUB_URL = BASE_URL + "OpticalBenchHub.htm"
DEFAULT_OUTPUT = ROOT / "camerae2e_db/sources/photons_to_photos"
DEFAULT_V9 = (
    ROOT
    / "camerae2e_db/lens_db/CameraE2E_Lens_DB_v9_20260627/data/lens_patents"
    / "lens_patent_simulation_v9.sqlite"
)
LICENSE = {
    "status": "all_rights_reserved_no_open_data_license_identified",
    "notice": "(C)opyright 2017-2026 William J. Claff All Rights Reserved.",
    "evidence_url": HUB_URL,
    "scope": (
        "Source website and curated prescription files; "
        "no broader reuse grant inferred from public access."
    ),
}

# A reviewable first batch, balancing modern primes, zooms, historical designs,
# and companies absent from the existing v9 company tables. Not random crawling.
DEFAULT_SELECTION = [
    ("JP2015-114366", "Example01P"),  # Sigma 50 Art
    ("JP2022-033487", "Example01P"),  # Sigma 35 DN Art
    ("JP2021-085935", "Example01P"),  # Sigma 85 DN
    ("JP2020-030383", "Example01P"),  # Sigma 56 DC DN
    ("JP2018-205527", "Example01P"),  # Sigma 16 DC DN
    ("JP2022-138408", "Example01P"),  # Sigma 18-50 zoom
    ("JP2024-062005", "Example02P"),  # Sigma 17 compact prime
    ("JP2019-144477", "Example01P"),  # Sigma 105 Art
    ("JP2023-074094", "Example01P"),  # Sigma 24 DN
    ("JP2012-234169", "Example01P"),  # Sigma 30 DN
    ("KR1020140184971", "Example01P"),  # Samyang 50
    ("WO2021-246545", "Example01P"),  # Samyang 18
    ("US20240151940", "Example01P"),  # Samyang 24/1.8
    ("WO2020-230915", "Example01P"),  # Samyang 24/2.8
    ("KR1020180065114", "Example04P"),  # Samyang 35
    ("KR1020200129711", "Example02P"),  # Samyang 85 EF
    ("KR1020150155317", "Example01P"),  # Samyang 135
    ("US20250231383", "Example03P"),  # Samyang 35-150 zoom
    ("US20220066176", "Example01P"),  # Leica 35 APO, source corrections retained
    ("US20240241349", "Example01P"),  # Leica 43 Q3
    ("US20160266350", "Example01P"),  # Leica 28 Q2
    ("US004123144", "Example09P"),  # Leica 50 Summicron
    ("JP2016-090725", "Example09P"),  # Leica 60 macro
    ("US20200341238", "Example03P"),  # Leica 18 TL
    ("JP2018-087903", "Example01P"),  # Leica 16-35 zoom
    ("US003874771", "Example05P"),  # Zeiss Planar 50
    ("JP2016-188967", "Example01P"),  # Zeiss Batis 18
    ("JP2015-096915", "Example02P"),  # Zeiss Batis 85
    ("US20180180842", "Example01P"),  # Zeiss ZX1 35
    ("US002186621", "Example01P"),  # Zeiss Sonnar 50
    ("US003994576", "Example01P"),  # Zeiss Sonnar 40
    ("US20130314585", "Example03P"),  # Zeiss RX100 compact zoom
]


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def normalized_patent(value: str) -> str:
    """Exact publication-number comparison only; does not merge patent families."""
    value = re.sub(r"[^A-Z0-9]", "", value.upper())
    value = re.sub(r"[ABC]\d?$", "", value)
    match = re.fullmatch(r"([A-Z]+)(\d+)", value)
    return match[1] + str(int(match[2])) if match else value


def existing_patents(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        return {
            normalized_patent(row[0])
            for row in connection.execute("SELECT DISTINCT publication_number FROM lenses")
        }


def parse_hub(raw: bytes, existing: set[str]) -> list[dict]:
    html = raw.decode("utf-8-sig")
    block = html.split("var modeldata = [", 1)[1].split("];", 1)[0]
    quoted = r"'((?:\\.|[^'])*)'"
    pattern = re.compile(
        r"\{\s*PatentID:\s*"
        + quoted
        + r",\s*ExampleID:\s*"
        + quoted
        + r",\s*TaleID:\s*"
        + quoted
        + r",\s*Description:\s*"
        + quoted
        + r"\s*\}"
    )
    rows = []
    for match in pattern.finditer(block):
        patent, example, tale, title = [
            ast.literal_eval("'" + value + "'") for value in match.groups()
        ]
        filename = "_".join(part for part in (patent, example, tale) if part) + ".txt"
        rows.append(
            {
                "publication_number": patent,
                "example_label": example,
                "tale_id": tale,
                "title": title,
                "brand_from_title": title.split()[0],
                "data_filename": filename,
                "source_url": urljoin(BASE_URL, "Data/" + filename),
                "viewer_url": BASE_URL
                + "OpticalBench.htm#Data/"
                + filename
                + ",figureOpacity=0.25,AxisO,OffAxis",
                "index_url": HUB_URL,
                "normalized_publication_number": normalized_patent(patent),
                "publication_present_in_v9": normalized_patent(patent) in existing,
                "match_scope": "exact_publication_number_only_not_patent_family",
            }
        )
    if len(rows) != len(re.findall(r"\bPatentID\s*:", block)):
        raise ValueError("Hub format changed: not all modeldata objects were parsed")
    if not rows:
        raise ValueError("Hub contains no parseable modeldata")
    return rows


def prescription_summary(raw: bytes) -> dict:
    text = raw.decode("utf-8-sig")
    if not text.startswith("[descriptive data]") or "[lens data]" not in text:
        raise ValueError("Response is not a readable public OpticalBench prescription")
    sections: dict[str, list[list[str]]] = {}
    section = ""
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            sections.setdefault(section, [])
        elif line.strip():
            sections.setdefault(section, []).append(line.split("\t"))
    variables = {row[0]: row[1:] for row in sections.get("variable distances", [])}
    lens_rows = sections.get("lens data", [])
    return {
        "surface_row_count": len(lens_rows),
        "aspherical_row_count": len(sections.get("aspherical data", [])),
        "configuration_column_count": max(
            (len(row) - 1 for row in sections.get("variable distances", [])), default=0
        ),
        "focal_length_raw": variables.get("Focal Length", []),
        "f_number_raw": variables.get("F-Number", []),
        "image_height_raw": variables.get("Image Height", []),
        "surface_type_tokens": sorted(
            {row[1] for row in lens_rows if len(row) > 1 and re.search(r"[A-Za-z]", row[1])}
        ),
        "section_names": list(sections),
        "notes_raw": sections.get("notes", []),
        "contains_undefined_literal": "undefined" in text.lower(),
        "has_source_original_aspheric_section": "aspherical data(p)" in sections,
        "simulation_ready": False,
        "readiness_note": (
            "Acquisition only. Source geometry, type tokens, configuration variables, "
            "dispersion, source corrections and aperture semantics require parser validation."
        ),
    }


class Fetcher:
    def __init__(self, output: Path, delay: float, refresh: bool, offline: bool):
        self.output, self.delay, self.refresh, self.offline = output, delay, refresh, offline
        self.last_request = 0.0
        cache_path = output / "fetch_metadata.json"
        self.cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    def fetch(self, url: str, relative_path: str) -> tuple[bytes, dict]:
        path = self.output / relative_path
        if path.exists() and (not self.refresh or self.offline):
            raw = path.read_bytes()
            old = self.cache.get(relative_path, {})
            checksum = hashlib.sha256(raw).hexdigest()
            if not old or old.get("sha256") != checksum or old.get("source_url") != url:
                raise ValueError(f"Cached file hash/source metadata mismatch: {relative_path}")
            return raw, {
                **old,
                "source_url": url,
                "path": relative_path,
                "sha256": checksum,
                "bytes": len(raw),
                "cache_reused": True,
            }
        if path.exists():
            raise ValueError(
                "Existing raw files are immutable; use a new --output for a new snapshot"
            )
        if self.offline:
            raise FileNotFoundError(f"Offline cache is missing {relative_path}")
        wait = self.delay - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.monotonic()
        request = Request(
            url,
            headers={"User-Agent": "CameraE2E-source-acquisition/1.0 (bounded public lens sample)"},
        )
        with urlopen(request, timeout=45) as response:
            raw = response.read(5_000_001)
            if len(raw) > 5_000_000:
                raise ValueError("Unexpectedly large response; stopped at 5 MB")
            metadata = {
                "source_url": url,
                "resolved_url": response.url,
                "path": relative_path,
                "retrieved_at_utc": now(),
                "http_status": response.status,
                "http_content_type": response.headers.get("Content-Type"),
                "http_last_modified": response.headers.get("Last-Modified"),
                "http_etag": response.headers.get("ETag"),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "cache_reused": False,
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
        self.cache[relative_path] = metadata
        write_json(self.output / "fetch_metadata.json", self.cache)
        return raw, metadata


def select_balanced(rows: list[dict], prior: list[dict], target: int) -> list[dict]:
    """Preserve prior identities, then fill least-represented title-derived brands."""
    by_url = {row["source_url"]: row for row in rows}
    selected = [{**old, **by_url.get(old["source_url"], {})} for old in prior]
    seen = {row["source_url"] for row in selected}
    # Keep the original curated seed selection reproducible for an empty cache.
    for key in DEFAULT_SELECTION:
        if len(selected) >= target:
            break
        candidates = [
            r
            for r in rows
            if (r["publication_number"], r["example_label"]) == key
            and not r["tale_id"]
            and r["source_url"] not in seen
        ]
        if candidates:
            selected.append(candidates[0])
            seen.add(candidates[0]["source_url"])

    def rank(row: dict) -> tuple:
        match = re.match(
            r"^(?:(?:US|JP|WO)|KR10)((?:19|20)\d{2})-?\d{5,}$",
            row["publication_number"],
        )
        year = int(match[1]) if match else 0
        return row["publication_present_in_v9"], -year, row["title"], row["source_url"]

    buckets: dict[str, list[dict]] = {}
    for row in sorted(rows, key=rank):
        if row["source_url"] not in seen:
            buckets.setdefault(row["brand_from_title"], []).append(row)
    counts = Counter(row["brand_from_title"] for row in selected)
    while len(selected) < target:
        available = [brand for brand, items in buckets.items() if items]
        if not available:
            break
        brand = min(available, key=lambda name: (counts[name], name.casefold()))
        row = buckets[brand].pop(0)
        selected.append(row)
        seen.add(row["source_url"])
        counts[brand] += 1
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--v9-db", type=Path, default=DEFAULT_V9)
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "reports/lens_source_expansion/p2p_acquisition.md",
    )
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument(
        "--limit",
        type=int,
        default=len(DEFAULT_SELECTION),
        help="Cumulative target: preserve acquired files, then balance title-derived brands",
    )
    parser.add_argument("--index-only", action="store_true")
    parser.add_argument(
        "--offline", action="store_true", help="Rebuild manifests/reports from cached bytes only"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch into a fresh output only; existing raw files are immutable",
    )
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    if args.delay_seconds < 0.5:
        parser.error("--delay-seconds must be at least 0.5")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    prior_path = output / "manifest.json"
    prior = json.loads(prior_path.read_text()) if prior_path.exists() else {}
    prior_files = prior.get("files", [])
    for row in prior_files:
        source_path = (output / row["path"]).resolve()
        if not source_path.is_relative_to(output):
            raise ValueError("Prior manifest source escapes output directory")
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError(f"Prior source hash mismatch: {row['path']}")
    prior_sha = {r["source_url"]: r["sha256"] for r in prior_files}
    fetcher = Fetcher(output, args.delay_seconds, args.refresh, args.offline)
    existing = existing_patents(args.v9_db)
    hub, hub_metadata = fetcher.fetch(HUB_URL, "raw/OpticalBenchHub.htm")
    _, bench_metadata = fetcher.fetch(BASE_URL + "OpticalBench.htm", "raw/OpticalBench.htm")
    rows = parse_hub(hub, existing)
    write_json(
        output / "index.json",
        {
            "source_url": HUB_URL,
            "source_snapshot": hub_metadata,
            "generated_at_utc": now(),
            "license": LICENSE,
            "v9_database": str(args.v9_db),
            "v9_comparison_scope": (
                "Exact normalized publication number, not family or geometry equality."
            ),
            "entries": rows,
        },
    )
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    (output / "index.csv").write_text(stream.getvalue())
    selected = [] if args.index_only else select_balanced(rows, prior_files, args.limit)
    write_json(output / "selection.json", selected)
    files, failures = list(prior_files), []
    for selection_index, row in enumerate(selected, 1):
        try:
            raw, metadata = fetcher.fetch(
                row["source_url"], "raw/prescriptions/" + row["data_filename"]
            )
            summary = prescription_summary(raw)
            entry = {
                **row,
                **metadata,
                "format": "p2p",
                "format_detail": "OpticalBench sectioned tab-delimited text",
                "license": LICENSE,
                "acquisition_method": "public_Data_link_reconstructed_from_hub_click_handler",
                "metadata": summary,
            }
            existing_index = next(
                (i for i, old in enumerate(files) if old["source_url"] == row["source_url"]), None
            )
            if existing_index is None:
                files.append(entry)
            else:
                files[existing_index] = entry
            # Save after each successful fetch so downstream consumers can start.
            write_json(
                output / "manifest.json",
                {
                    "schema_version": 1,
                    "source": "photons_to_photos",
                    "created_at_utc": prior.get("created_at_utc") or now(),
                    "license": LICENSE,
                    "index_snapshot": hub_metadata,
                    "viewer_snapshot": bench_metadata,
                    "files": files,
                    "failures": failures,
                },
            )
            print(
                f"{selection_index:02d}/{len(selected):02d} {row['title']} ({len(raw)} bytes)",
                flush=True,
            )
        except Exception as exc:
            failures.append({**row, "error": str(exc), "failed_at_utc": now()})
            print(f"FAILED {row['data_filename']}: {exc}", flush=True)
    manifest = {
        "schema_version": 1,
        "source": "photons_to_photos",
        "created_at_utc": prior.get("created_at_utc") or now(),
        "license": LICENSE,
        "index_snapshot": hub_metadata,
        "viewer_snapshot": bench_metadata,
        "selection_policy": (
            "Cumulative acquisition: retain all previous files and curated 32-entry seed; "
            "then fill least-represented title-derived brands, preferring publications "
            "without exact v9 matches and more recent publication years within each brand. "
            "Only published index entries are selected; no blind scanning."
        ),
        "selection_requested_total": args.limit,
        "files": files,
        "failures": failures,
    }
    write_json(output / "manifest.json", manifest)
    counts = Counter(row["brand_from_title"] for row in rows)
    acquired_urls = {r["source_url"] for r in files}
    indexed_urls = {r["source_url"] for r in rows}
    preserved = all(
        next(r["sha256"] for r in files if r["source_url"] == url) == sha
        for url, sha in prior_sha.items()
    )
    summary = {
        "generated_at_utc": now(),
        "selection_requested_total": args.limit,
        "prior_verified_count": len(prior_files),
        "prior_source_hashes_preserved": preserved,
        "added_this_run": len(acquired_urls - set(prior_sha)),
        "remaining_indexed_prescriptions": len(indexed_urls - acquired_urls),
        "requested_priority_brands_in_index": {
            name: sum(bool(re.search(name, r["title"], re.I)) for r in rows)
            for name in ("Apple", "Genius")
        },
        "source_url": HUB_URL,
        "index_entries": len(rows),
        "unique_index_prescription_keys": len(
            {(r["publication_number"], r["example_label"], r["tale_id"]) for r in rows}
        ),
        "unique_index_publications": len({r["publication_number"] for r in rows}),
        "v9_unique_publications": len(existing),
        "index_entries_with_exact_publication_match_in_v9": sum(
            r["publication_present_in_v9"] for r in rows
        ),
        "index_entries_without_exact_publication_match_in_v9": sum(
            not r["publication_present_in_v9"] for r in rows
        ),
        "brand_counts_from_titles": dict(counts.most_common()),
        "downloaded_prescriptions": len(files),
        "downloaded_bytes": sum(r["bytes"] for r in files),
        "downloaded_surface_rows": sum(r["metadata"]["surface_row_count"] for r in files),
        "downloaded_aspherical_rows": sum(r["metadata"]["aspherical_row_count"] for r in files),
        "downloaded_configuration_columns": sum(
            r["metadata"]["configuration_column_count"] for r in files
        ),
        "downloaded_brand_counts": dict(Counter(r["brand_from_title"] for r in files)),
        "downloaded_with_exact_publication_match_in_v9": sum(
            r["publication_present_in_v9"] for r in files
        ),
        "download_failures": failures,
        "phone_labeled_entries": [
            r
            for r in rows
            if re.search(r"phone|mobile|iphone|pixel|Largan|Sunny|Samsung Galaxy", r["title"], re.I)
        ],
        "license": LICENSE,
        "limitations": [
            (
                "Hub descriptions associate patent examples with commercial lenses; "
                "production equivalence is unverified."
            ),
            (
                "Source prescriptions can include author repairs, inferred dimensions "
                "and disputed aspheres; notes are preserved."
            ),
            (
                "Exact publication non-overlap does not prove patent-family "
                "or prescription non-overlap."
            ),
            (
                "Acquisition is not a simulation-readiness claim; "
                "no PSF validation was performed here."
            ),
            (
                "Full diameter versus semi-diameter, asphere order, stop tokens "
                "and variable configurations require explicit normalization."
            ),
            (
                "Source notice reserves rights; no permissive data license "
                "or redistribution permission was found."
            ),
        ],
    }
    write_json(output / "summary.json", summary)
    report_dir = args.report.parent
    report_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.report.with_suffix(".json"), summary)
    report = (
        f"""# Photons to Photos cumulative acquisition

Source: [{HUB_URL}]({HUB_URL})

Indexed {len(rows)} public hub entries and verified {len(files)} readable OpticalBench
prescriptions
({summary["downloaded_bytes"]:,} bytes; {summary["downloaded_surface_rows"]} surface rows;
{summary["downloaded_aspherical_rows"]} aspherical rows). {len(failures)} download failures.

Selected brands: {json.dumps(summary["downloaded_brand_counts"])}.
Cumulative target: {args.limit}; remaining indexed prescriptions:
{summary["remaining_indexed_prescriptions"]}. Previous verified files: {len(prior_files)};
all previous source hashes preserved:
{preserved}. Apple/Genius title matches: {summary["requested_priority_brands_in_index"]}.
The index contains {summary["unique_index_publications"]} distinct publication identifiers.
{summary["index_entries_without_exact_publication_match_in_v9"]} hub entries lack an exact
normalized publication-number match among v9's {len(existing)} publications.
This is not a patent-family or geometry deduplication result.

## Files and repeatability

- `camerae2e_db/sources/photons_to_photos/index.json` and `index.csv`: the full publicly displayed
  hub index, patent/example IDs, titles and supported viewer/data URLs.
- `camerae2e_db/sources/photons_to_photos/manifest.json`: each raw prescription's path, URL,
  UTC acquisition time, SHA-256, byte count, HTTP metadata, format, rights status
  and raw source notes.
- `raw/OpticalBenchHub.htm` and `raw/OpticalBench.htm`: byte-preserved index
  and viewer-page provenance.
- `raw/prescriptions/*.txt`: byte-preserved prescription files.
  No remotely hosted JavaScript was executed or decrypted by this acquisition.
- `python3 tools/acquire_p2p_lenses.py --limit 200`: cumulative target 200, verified cache reuse,
  default one-second request spacing, and deterministic brand balancing after the curated seed.
- Existing source bytes are immutable; use a fresh output for another index snapshot.
- `--report PATH` selects the report path without overwriting earlier acquisition reports.
- `python3 tools/acquire_p2p_lenses.py --offline`: rebuild metadata/reports without network access.

## Material limits

"""
        + "\n".join("- " + item for item in summary["limitations"])
        + "\n"
    )
    args.report.write_text(report)
    print(
        json.dumps(
            {
                key: value
                for key, value in summary.items()
                if key
                in (
                    "index_entries",
                    "downloaded_prescriptions",
                    "downloaded_surface_rows",
                    "downloaded_aspherical_rows",
                    "downloaded_brand_counts",
                    "download_failures",
                )
            },
            indent=2,
        )
    )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
