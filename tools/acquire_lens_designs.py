#!/usr/bin/env python3
"""Acquire a cumulative, reproducible public lens-designs.com ZMX selection.

Only the publisher's linked public Sheets/Drive download endpoints are used.
Raw responses and source files are immutable and verified by SHA256 on reuse.
No downloaded content is executed. A manifest records every selection/failure.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "camerae2e_db/sources/lens_designs"
REPORT = ROOT / "reports/lens_source_expansion/lens_designs_acquisition.md"
SITE = "https://www.lens-designs.com"
CATALOGS = {
    "primes": {
        "page": SITE + "/photographic-primes",
        "sheet": "1yv4tIaQhDoNYmi22A8jiNKpmtBKxMv3DBOJIwDKq3kU",
        "folder": "1xF3gfK75Vz6D4Kgtmkc2MslcdUU7sHq1",
        "basis_column": "Basis",
    },
    "zooms": {
        "page": SITE + "/photographic-zooms",
        "sheet": "1S57Ncj1fOVWGl5XgkUOsgh6rf0QW-rPD4XOnONz4cng",
        "folder": "15c6r3BzTFuLuvoOm7qbE93Y7dpOcSgKf",
        "basis_column": "Design basis",
    },
}


def utcnow() -> str:
    # Acquisition intentionally also runs with macOS system Python 3.9.
    return datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: UP017


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text() == data:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    tmp.replace(path)


class Acquisition:
    def __init__(self, root: Path, delay: float, offline: bool):
        self.root, self.delay, self.offline = root, delay, offline
        self.last_request = 0.0

    def get(self, relative_path: str, url: str) -> tuple[bytes, dict]:
        path = self.root / relative_path
        sidecar = path.with_suffix(path.suffix + ".source.json")
        if path.exists():
            if not sidecar.exists():
                raise ValueError(f"Refusing untracked existing source file: {path}")
            info = json.loads(sidecar.read_text())
            data = path.read_bytes()
            if info["sha256"] != digest(data) or info["source_url"] != url:
                raise ValueError(f"Cached file integrity/source mismatch: {path}")
            return data, info
        if self.offline:
            raise FileNotFoundError(f"Missing offline source: {path}")
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        request = urllib.request.Request(
            url, headers={"User-Agent": "CameraE2E-Lens-Acquisition/1.1"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(20_000_001)
            if len(data) > 20_000_000:
                raise ValueError("Response exceeds 20 MB acquisition limit")
            info = {
                "source_url": url,
                "resolved_url": response.url,
                "retrieved_at": utcnow(),
                "status_code": response.status,
                "content_type": response.headers.get("Content-Type"),
                "bytes": len(data),
                "sha256": digest(data),
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation: existing raw downloads are never overwritten.
        with path.open("xb") as stream:
            stream.write(data)
        dump(sidecar, info)
        return data, info


def stem_key(value: str) -> str:
    return re.sub(r"[\s_]+", "_", re.sub(r"\.zmx$", "", value.strip(), flags=re.I)).lower()


def parse_folder(blob: bytes) -> list[dict]:
    pattern = (
        r'<div class="flip-entry" id="entry-([^\"]+)"[^>]*>'
        r'.*?<div class="flip-entry-title">(.*?)</div>'
    )
    entries = [
        {"drive_id": match[1], "filename": html.unescape(match[2])}
        for match in re.finditer(pattern, blob.decode("utf-8"), re.S)
    ]
    if not entries:
        raise ValueError("No public Drive entries found; HTML format may have changed")
    return entries


def decode_zmx(blob: bytes) -> tuple[str, str]:
    encoding = "utf-16" if blob.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = blob.decode(encoding)
    except UnicodeDecodeError:
        encoding, text = "cp1252", blob.decode("cp1252")
    if not re.match(r"^VERS\s", text) or not re.search(r"^SURF\s+\d+", text, re.M):
        raise ValueError("Downloaded response is not a recognizable ZMX prescription")
    return text, encoding


def license_evidence(note_lines: list[str], relative_path: str) -> dict:
    """Per-file notices override the website default; never infer MIT from generic permission."""
    notes = "\n".join(note_lines)
    explicit_mit = bool(re.search(r"\bMIT License\b", notes, re.I))
    other_notice = bool(re.search(r"copyright|permission|rights reserved|license", notes, re.I))
    if explicit_mit:
        identifier, status = "MIT", "embedded_notice"
    elif other_notice:
        identifier, status = None, "embedded_custom_notice_review_required"
    else:
        identifier, status = "MIT", "publisher_default_no_embedded_notice"
    return {
        "identifier": identifier,
        "status": status,
        "source_url": SITE + "/note-on-ip",
        "evidence_path": relative_path if other_notice else "metadata/license.html",
        "publisher_default_identifier": "MIT",
        "publisher_evidence_path": "metadata/license.html",
        "scope": "design_file; no patent clearance or product-equivalence assertion",
        "embedded_notice_detected": other_notice,
        "notice_text": notes if other_notice else None,
    }


def identifiers(filename: str) -> tuple[str | None, str | None]:
    name = Path(filename).stem
    match = re.match(r"^((?:US|EP|JP|CN|WO|KR|DE|GB|CH)\d+(?:-\d{5,})?[A-C]?\d?)(.*)$", name, re.I)
    if not match:
        return None, None
    tail = match[2]
    example = re.search(r"(?:Example|ex)[ _-]?(\d+)", tail, re.I)
    if not example:
        example = re.match(r"-(\d+)$", tail)
    return match[1].upper(), (f"Example {int(example[1])}" if example else None)


def select(
    catalog: dict[str, list[dict]],
    mobile: int,
    primes: int,
    zooms: int,
    prior: list[dict] | None = None,
) -> list[dict]:
    """Keep acquired files, then fill category quotas from published metadata."""
    prior = prior or []

    def cumulative(rows: list[dict], category: str, quota: int) -> list[dict]:
        current = {r["drive_id"]: r for r in rows}
        chosen = [
            {**r, **current.get(r["drive_id"], {}), "category": category}
            for r in prior
            if r["category"] == category
        ]
        seen = {r["drive_id"] for r in chosen}
        for row in rows:
            if len(chosen) >= quota:
                break
            if row["drive_id"] not in seen:
                chosen.append({**row, "category": category})
                seen.add(row["drive_id"])
        return chosen

    def interleave_countries(rows: list[dict]) -> list[dict]:
        buckets: dict[str, list[dict]] = {}
        for row in rows:
            match = re.match(r"[A-Za-z]+", row["filename"])
            country = match[0].upper() if match else "OTHER"
            buckets.setdefault(country, []).append(row)
        result = []
        while any(buckets.values()):
            for key in sorted(buckets):
                if buckets[key]:
                    result.append(buckets[key].pop(0))
        return result

    mobile_rows = [
        r
        for r in catalog["primes"]
        if re.search(r"mobile|phone", r["metadata"].get("notes", ""), re.I)
    ]
    preferred = [
        "EP1357414A1_ex1",
        "US6844989",
        "US7643225",
        "US8558939",
        "US11163141_lens system 610_folded",
        "US08934179-4",
    ]
    rank = {stem_key(v): i for i, v in enumerate(preferred)}
    mobile_rows.sort(key=lambda r: (rank.get(stem_key(r["filename"]), 999), r["filename"]))
    top = [r for r in mobile_rows if stem_key(r["filename"]) in rank][:mobile]
    remaining = [r for r in mobile_rows if r not in top]
    need = min(max(0, mobile - len(top)), len(remaining))
    if need:
        top.extend(
            remaining[round(i * (len(remaining) - 1) / max(1, need - 1))] for i in range(need)
        )
    for row in top:
        row["category"] = "mobile_imaging"
    preferred_primes = [
        "JP2014-048488_Example03P",
        "US20160282590_Example01P",
        "JP2011-048232_Example03P",
        "CN104101985_Example01P",
        "KR1020180084767_Example03P",
        "WO2020-158622_Example03P",
        "US008416512_Example03P",
        "US20130057972_Example03P",
        "US006560042_Example03P",
        "JP2020-173366_Example03P",
    ]
    prime_rank = {stem_key(v): i for i, v in enumerate(preferred_primes)}
    prime_rows = [
        r
        for r in catalog["primes"]
        if r not in mobile_rows and r["metadata"].get("efl (mm)", "") not in ("NA", "")
    ]
    prime_rows.sort(key=lambda r: (prime_rank.get(stem_key(r["filename"]), 999), r["filename"]))
    selected_primes = prime_rows[:primes]
    for row in selected_primes:
        row["category"] = "photographic_prime"
    # Prefer complete prescription-based zooms over schematically reconstructed
    # historical examples, while including compact and larger photographic zooms.
    preferred_zooms = ["US05825556-1", "US06710934-1", "US07085067-1"]
    zoom_rank = {stem_key(v): i for i, v in enumerate(preferred_zooms)}
    zoom_rows = list(catalog["zooms"])
    zoom_rows.sort(
        key=lambda r: (
            zoom_rank.get(
                stem_key(r["filename"]),
                10
                if re.search(r"24[-–]70|18[-–]55|70[-–]200", r["metadata"].get("notes", ""))
                else 999,
            ),
            r["filename"],
        )
    )
    selected_zooms = zoom_rows[:zooms]
    for row in selected_zooms:
        row["category"] = "photographic_zoom"
    return (
        cumulative(top + mobile_rows, "mobile_imaging", mobile)
        + cumulative(
            selected_primes[: len(preferred_primes)] + interleave_countries(prime_rows),
            "photographic_prime",
            primes,
        )
        + cumulative(
            selected_zooms[: len(preferred_zooms)] + interleave_countries(zoom_rows),
            "photographic_zoom",
            zooms,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SOURCE)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument(
        "--total",
        type=int,
        help="Cumulative target: indexed mobiles first, then balanced primes/zooms",
    )
    parser.add_argument("--mobile", type=int, default=20)
    parser.add_argument("--primes", type=int, default=10)
    parser.add_argument("--zooms", type=int, default=6)
    parser.add_argument("--delay", type=float, default=0.4)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Revalidate cached acquisition without network requests",
    )
    args = parser.parse_args()
    if min(args.mobile, args.primes, args.zooms) < 0 or not 0 <= args.delay <= 60:
        parser.error("Counts must be nonnegative and delay must be between 0 and 60 seconds")
    if args.total is not None and args.total < 0:
        parser.error("--total must be nonnegative")
    args.output = args.output.resolve()
    prior_path = args.output / "manifest.json"
    prior = json.loads(prior_path.read_text()) if prior_path.exists() else {}
    prior_files = prior.get("files", [])
    for row in prior_files:
        source_path = (args.output / row["path"]).resolve()
        if not source_path.is_relative_to(args.output):
            raise ValueError("Prior manifest source escapes output directory")
        if digest(source_path.read_bytes()) != row["sha256"]:
            raise ValueError(f"Prior source hash mismatch: {row['path']}")
    prior_sha = {r["source_url"]: r["sha256"] for r in prior_files}
    prior_by_drive = {r["drive_id"]: r for r in prior_files}
    acq = Acquisition(args.output, args.delay, args.offline)
    manifest = {
        "schema_version": 1,
        "source": "lens-designs.com",
        "source_home": SITE + "/",
        "selection_policy": "cumulative_mobile_priority_balanced_prime_zoom_country_interleaving",
        "raw_metadata": [],
        "catalog_statistics": {},
        "files": list(prior_files),
        "failures": [],
    }
    for label, url in [
        ("home", SITE + "/"),
        ("downloading", SITE + "/downloading-files"),
        ("license", SITE + "/note-on-ip"),
    ]:
        _, info = acq.get(f"metadata/{label}.html", url)
        manifest["raw_metadata"].append({"path": f"metadata/{label}.html", **info})
    catalog = {}
    for kind, definition in CATALOGS.items():
        page_blob, info = acq.get(f"metadata/{kind}.html", definition["page"])
        manifest["raw_metadata"].append({"path": f"metadata/{kind}.html", **info})
        if (
            definition["sheet"].encode() not in page_blob
            or definition["folder"].encode() not in page_blob
        ):
            raise ValueError(
                f"Configured {kind} Sheet/Drive IDs are no longer linked by publisher page"
            )
        csv_path = f"metadata/{kind}.csv"
        csv_blob, info = acq.get(
            csv_path,
            f"https://docs.google.com/spreadsheets/d/{definition['sheet']}/export?format=csv",
        )
        manifest["raw_metadata"].append({"path": csv_path, **info})
        rows = list(csv.DictReader(io.StringIO(csv_blob.decode("utf-8-sig"))))
        metadata = {stem_key(r[definition["basis_column"]]): r for r in rows}
        folder_path = f"metadata/{kind}_folder.html"
        folder_url = f"https://drive.google.com/embeddedfolderview?id={definition['folder']}#list"
        folder_blob, info = acq.get(folder_path, folder_url)
        manifest["raw_metadata"].append({"path": folder_path, **info})
        entries = parse_folder(folder_blob)
        for row in entries:
            row.update(
                {
                    "metadata": metadata.get(stem_key(row["filename"]), {}),
                    "catalog": kind,
                    "catalog_url": definition["page"],
                    "metadata_source_url": f"https://docs.google.com/spreadsheets/d/{definition['sheet']}/edit",
                    "folder_url": folder_url,
                }
            )
        catalog[kind] = [
            r for r in entries if r["filename"].lower().endswith(".zmx") and r["metadata"]
        ]
        manifest["catalog_statistics"][kind] = {
            "metadata_rows": len(rows),
            "folder_entries": len(entries),
            "folder_formats": dict(Counter(Path(r["filename"]).suffix.lower() for r in entries)),
            "zmx_with_matched_metadata": len(catalog[kind]),
        }
        dump(args.output / f"metadata/{kind}_index.json", entries)
    mobile_available = sum(
        bool(re.search(r"mobile|phone", r["metadata"].get("notes", ""), re.I))
        for r in catalog["primes"]
    )
    if args.total is not None:
        prior_counts = Counter(r["category"] for r in prior_files)
        args.mobile = max(prior_counts["mobile_imaging"], min(args.total, mobile_available))
        remainder = max(0, args.total - args.mobile)
        args.primes = max(prior_counts["photographic_prime"], (remainder + 1) // 2)
        args.zooms = max(prior_counts["photographic_zoom"], remainder // 2)
    selection = select(catalog, args.mobile, args.primes, args.zooms, prior_files)
    manifest["selection_request"] = {
        "total": args.total,
        "mobile": args.mobile,
        "primes": args.primes,
        "zooms": args.zooms,
    }
    manifest["prior_verified_count"] = len(prior_files)
    dump(args.output / "selection.json", selection)
    for index, row in enumerate(selection, 1):
        filename = row["filename"]
        if Path(filename).name != filename:
            raise ValueError("Unsafe source filename")
        relative = f"raw/{row['category']}/{filename}"
        url = f"https://drive.google.com/uc?export=download&id={row['drive_id']}"
        try:
            blob, info = acq.get(relative, url)
            text, encoding = decode_zmx(blob)
            patent, example = identifiers(filename)
            note_lines = [line for line in text.splitlines() if re.match(r"^NOTE\b", line)]
            license_record = license_evidence(note_lines, relative)
            old_license = prior_by_drive.get(row["drive_id"], {}).get("license", {})
            if all(
                old_license.get(key) == license_record.get(key) for key in ("identifier", "status")
            ):
                # Retain established provenance verbatim unless its classification is incorrect.
                license_record = old_license
            file_entry = {
                **row,
                **info,
                "path": relative,
                "format": "zmx",
                "encoding": encoding,
                "title": row["metadata"].get("notes") or Path(filename).stem,
                "publication_number": patent,
                "example_label": example,
                "license": license_record,
                "source_notes": note_lines,
                "structural_summary": {
                    "surface_count": len(re.findall(r"^SURF\s+\d+", text, re.M)),
                    "surface_types": sorted(set(re.findall(r"^\s*TYPE\s+(\S+)", text, re.M))),
                    "mode": next(iter(re.findall(r"^MODE\s+(\S+)", text, re.M)), None),
                    "has_stop": bool(re.search(r"^\s*STOP\s*$", text, re.M)),
                    "has_curvature": bool(re.search(r"^\s*CURV\s", text, re.M)),
                    "has_thickness": bool(re.search(r"^\s*DISZ\s", text, re.M)),
                    "has_glass": bool(re.search(r"^\s*GLAS\s", text, re.M)),
                },
                "validation_status": (
                    "source_download_verified; optical_normalization_and_raytrace_pending"
                ),
            }
            existing_index = next(
                (
                    i
                    for i, old in enumerate(manifest["files"])
                    if old["drive_id"] == row["drive_id"]
                ),
                None,
            )
            if existing_index is None:
                manifest["files"].append(file_entry)
            else:
                manifest["files"][existing_index] = file_entry
            print(f"[{index}/{len(selection)}] verified {filename} ({len(blob)} bytes)", flush=True)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            manifest["failures"].append(
                {**row, "source_url": url, "error": str(exc), "observed_at": utcnow()}
            )
            print(f"[{index}/{len(selection)}] FAILED {filename}: {exc}", flush=True)
        dump(args.output / "manifest.json", manifest)
    indexed_urls = {
        f"https://drive.google.com/uc?export=download&id={r['drive_id']}"
        for entries in catalog.values()
        for r in entries
    }
    acquired_urls = {r["source_url"] for r in manifest["files"]}
    manifest["prior_source_hashes_preserved"] = all(
        next(r["sha256"] for r in manifest["files"] if r["source_url"] == url) == sha
        for url, sha in prior_sha.items()
    )
    manifest["counts"] = {
        "indexed_zmx_folder_entries": sum(
            item["folder_formats"].get(".zmx", 0)
            for item in manifest["catalog_statistics"].values()
        ),
        "indexed_zmx_with_metadata": len(indexed_urls),
        "indexed_mobile_zmx": mobile_available,
        "remaining_indexed_zmx_with_metadata": len(indexed_urls - acquired_urls),
        "added_this_run": len(acquired_urls - set(prior_sha)),
        "selected": len(selection),
        "downloaded_verified": len(manifest["files"]),
        "failed": len(manifest["failures"]),
        "categories": dict(Counter(r["category"] for r in manifest["files"])),
        "license_statuses": dict(Counter(r["license"]["status"] for r in manifest["files"])),
    }
    manifest["limitations"] = [
        "Public download and recognizable ZMX structure do not establish simulation readiness "
        "or production lens equivalence.",
        "Publisher-authored materials, diameters, field definitions, and apertures may be "
        "reconstructed or optimized.",
        "Raw Zemax files may require catalog glasses, solve evaluation, or "
        "coordinate-break/multiconfiguration support.",
        "Publisher-default MIT license is recorded separately from embedded per-file evidence; "
        "custom notices override that default and are marked for review rather than labeled MIT. "
        "Original source files and notices are retained.",
        "Patent IDs retain source spelling/kind-code availability; "
        "patent family/legal status is not inferred.",
    ]
    dump(args.output / "manifest.json", manifest)
    lines = [
        "# lens-designs.com cumulative acquisition",
        "",
        f"Verified files: {len(manifest['files'])}; failures: {len(manifest['failures'])}.",
        "",
        "Sources: [download instructions](https://www.lens-designs.com/downloading-files), "
        "[primes](https://www.lens-designs.com/photographic-primes), "
        "[zooms](https://www.lens-designs.com/photographic-zooms), "
        "[publisher license](https://www.lens-designs.com/note-on-ip).",
        "",
        "The manifest and per-response sidecars preserve retrieval timestamps, original URLs, "
        "resolved URLs, SHA256 hashes, source metadata, and licensing evidence. "
        "Raw responses are never overwritten. "
        "Only public, linked endpoints were accessed; no account or API key is used.",
        "",
        "Run `python3 tools/acquire_lens_designs.py --offline` to revalidate the preserved "
        "downloads. Run without `--offline` to resume missing requests. "
        "Set `--total 200` for all mobile designs then balanced primes/zooms; or change "
        "`--mobile`, `--primes`, or `--zooms` for category quotas. Previously acquired files "
        "remain in the cumulative manifest. Use `--report` to choose a report path, "
        "or use a fresh `--output` directory for a new metadata snapshot.",
        "",
        "## Catalog snapshot",
        "",
        "| Catalog | CSV rows | Folder entries | Matched ZMX |",
        "|---|---:|---:|---:|",
    ]
    for k, v in manifest["catalog_statistics"].items():
        lines.append(
            f"| {k} | {v['metadata_rows']} | {v['folder_entries']} | "
            f"{v['zmx_with_matched_metadata']} |"
        )
    lines.extend(
        [
            "",
            "## Selected files",
            "",
            "| Category | Filename | Surfaces | License evidence |",
            "|---|---|---:|---|",
        ]
    )
    for row in manifest["files"]:
        lines.append(
            f"| {row['category']} | {row['filename']} | "
            f"{row['structural_summary']['surface_count']} | {row['license']['status']} |"
        )
    lines.extend(["", "## Limitations", "", *["- " + s for s in manifest["limitations"]]])
    if manifest["failures"]:
        lines.extend(
            [
                "",
                "## Acquisition failures",
                "",
                *[f"- {r['filename']}: {r['error']}" for r in manifest["failures"]],
            ]
        )
    lines.extend(
        [
            "",
            "## Cumulative coverage",
            "",
            f"Indexed metadata-linked ZMX: {len(indexed_urls)}; remaining: "
            f"{len(indexed_urls - acquired_urls)}. Mobile ZMX in index: {mobile_available}.",
            f"All {len(prior_files)} previously acquired file hashes preserved: "
            f"{manifest['prior_source_hashes_preserved']}.",
        ]
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n")
    dump(
        args.report.with_suffix(".json"),
        {
            "source": manifest["source"],
            "counts": manifest["counts"],
            "prior_verified_count": len(prior_files),
            "prior_source_hashes_preserved": manifest["prior_source_hashes_preserved"],
            "selection_request": manifest["selection_request"],
            "failures": manifest["failures"],
            "manifest": str(args.output / "manifest.json"),
        },
    )
    print(json.dumps(manifest["counts"], sort_keys=True), flush=True)
    return 1 if manifest["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
