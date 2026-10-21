#!/usr/bin/env python3
"""Archive a bounded batch of official EP lens patents and JP/KR discovery queue.

EP format links come from the documented Publication Server REST format index.
Only raw XML/PDF and lossless table evidence are acquired: no lens prescriptions
are normalized, no family relationship is inferred, and no catalog is modified.
Run again with --offline to verify and reproduce all derived outputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REST = "https://data.epo.org/publication-server/rest/v1.2/patents/"
EPO_HELP = "https://www.epo.org/en/searching-for-patents/technical/publication-server/help"
GUIDE = "https://data.epo.org/publication-server/doc/EPS%20REST%20services.pdf"
OFFICIAL_CANDIDATES = [
    (
        "EP2738586A1",
        "Imaging lens and imaging device using same",
        REST + "EP2738586NWA1/document.pdf",
    ),
    (
        "EP3286591A1",
        "Camera lens system with five lens components (Apple)",
        "https://www.epo.org/bulletin/downloads/2018/bulletin1809.pdf",
    ),
    (
        "EP3286592A1",
        "Zoom lens and image pickup apparatus (Canon)",
        "https://www.epo.org/bulletin/downloads/2018/bulletin1809.pdf",
    ),
    (
        "EP3865925B1",
        "Optical imaging lens and imaging device",
        "https://data.epo.org/publication-server/rest/v1.0/publication-dates/20230816/patents/EP3865925NWB1/document.pdf",
    ),
    (
        "EP3865926B1",
        "Vehicle-mounted camera lens and imaging device",
        "https://data.epo.org/publication-server/rest/v1.0/publication-dates/20230816/patents/EP3865926NWB1/document.pdf",
    ),
    (
        "EP4148477A1",
        "Imaging lens system",
        "https://data.epo.org/publication-server/rest/v1.0/publication-dates/20230315/patents/EP4148477NWA1/document.pdf",
    ),
    (
        "EP4365656A1",
        "Optical lens numerical prescriptions",
        "https://data.epo.org/publication-server/rest/v1.2/publication-dates/2024-05-08/patents/EP4365656NWA1/document.pdf",
    ),
]
SERVICE_EVIDENCE = [
    {
        "service": "EPO Publication Server",
        "url": "https://www.epo.org/en/searching-for-patents/data/web-services/publication-server",
        "status": "public_no_auth_route_verified_by_successful_document_download",
        "detail": (
            "Documented REST 1.2 supports XML and PDF without registration. Website "
            "currently says 5 GB per 7 days; the older PDF guide says 10 GB. This "
            "bounded batch follows the smaller limit."
        ),
        "cache_path": "metadata/epo_publication_server.html",
    },
    {
        "service": "EPO REST reference guide",
        "url": GUIDE,
        "status": "official_route_documentation",
        "detail": (
            "Revision 1.6 (2024-04) says user-agent registration is no longer "
            "necessary; section 3.3 specifies format-index discovery."
        ),
        "cache_path": "metadata/epo_rest_guide.pdf",
    },
    {
        "service": "EPO source fidelity",
        "url": EPO_HELP,
        "status": "source_fidelity_limitation",
        "detail": (
            "Pre-2006 XML is converted and may contain replication errors. PDF is "
            "also archived and must be checked before trusting extracted numeric "
            "tables. HTML is a convenience rendering."
        ),
        "cache_path": "metadata/epo_help.html",
    },
    {
        "service": "EPO OPS",
        "url": "https://www.epo.org/en/searching-for-patents/data/web-services/ops",
        "status": "registration_and_oauth_required_not_attempted",
        "detail": (
            "Official getting-started instructions require registration, app "
            "credentials, and OAuth. Use Publication Server for this no-auth EP "
            "batch."
        ),
        "cache_path": "metadata/epo_ops.html",
    },
    {
        "service": "JPO Patent Information Retrieval API",
        "url": "https://www.jpo.go.jp/system/laws/sesaku/data/api-provision.html",
        "status": "registration_required_not_attempted",
        "detail": (
            "Official page updated 2026-04-15 requires user registration and an "
            "application by email for domestic APIs. No registration or message was "
            "sent."
        ),
        "cache_path": "metadata/jpo_api.html",
    },
    {
        "service": "J-PlatPat",
        "url": "https://www.jpo.go.jp/e/support/j_platpat/patent_search.html",
        "status": "public_search_route_manual_verification_queue",
        "detail": (
            "Public patent-number search is the fallback. No verified no-auth bulk "
            "document API was found; the JPO English help URL returned HTTP 403 "
            "during this run. This does not imply J-PlatPat requires a paid account."
        ),
        "cache_path": "metadata/jplatpat_help.html",
    },
    {
        "service": "KIPRIS Plus",
        "url": "https://plus.kipris.or.kr/eng/main/contents.do?menuNo=300026",
        "status": "service_application_required_not_attempted",
        "detail": (
            "Official OpenAPI/bulk workflow shows sign-in, service selection, order "
            "and payment steps. Actual entitlement/fee depends on the dataset; no "
            "universal paid/free assertion is made. Public KIPRIS search remains a "
            "manual fallback."
        ),
        "cache_path": "metadata/kipris_plus.html",
    },
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: UP017


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text() == payload:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload)
    temporary.replace(path)


class Fetcher:
    def __init__(self, root: Path, delay: float, offline: bool):
        self.root, self.delay, self.offline = root, delay, offline
        self.last_request = 0.0

    def get(self, relative: str, url: str) -> tuple[bytes, dict]:
        path = self.root / relative
        sidecar = path.with_suffix(path.suffix + ".source.json")
        failure_sidecar = path.with_suffix(path.suffix + ".error.json")
        if path.exists():
            metadata = json.loads(sidecar.read_text())
            content = path.read_bytes()
            if metadata["source_url"] != url or metadata["sha256"] != sha256(content):
                raise ValueError(f"Immutable cache integrity/source mismatch: {path}")
            return content, metadata
        if self.offline:
            if failure_sidecar.exists():
                failure = json.loads(failure_sidecar.read_text())
                if failure["source_url"] != url:
                    raise ValueError("Cached failure source URL mismatch")
                raise RuntimeError(failure["error"])
            raise FileNotFoundError(f"Uncached source: {relative}")
        time.sleep(max(0.0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        request = urllib.request.Request(
            url, headers={"User-Agent": "CameraE2E-Public-Patent-Archive/1.0"}
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                content = response.read(32_000_001)
                if len(content) > 32_000_000:
                    raise ValueError("Response exceeds bounded 32 MB download size")
                metadata = {
                    "source_url": url,
                    "resolved_url": response.url,
                    "retrieved_at": utcnow(),
                    "status_code": response.status,
                    "content_type": response.headers.get("Content-Type", ""),
                    "bytes": len(content),
                    "sha256": sha256(content),
                    "path": relative,
                }
        except urllib.error.URLError as exc:
            write_json(
                failure_sidecar,
                {
                    "source_url": url,
                    "attempted_at": utcnow(),
                    "error": str(exc),
                    "status_code": getattr(exc, "code", None),
                },
            )
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
        write_json(sidecar, metadata)
        return content, metadata


def normalize_publication(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def build_candidates(source_root: Path) -> tuple[list[dict], list[dict], list[dict]]:
    candidates: dict[str, dict] = {}
    inputs = []
    paths = [
        source_root / "lens_designs/metadata/primes_index.json",
        source_root / "lens_designs/metadata/zooms_index.json",
        source_root / "photons_to_photos/index.json",
    ]
    for path in paths:
        data = path.read_bytes()
        inputs.append({"path": str(path.relative_to(ROOT)), "sha256": sha256(data)})
        payload = json.loads(data)
        rows = payload if isinstance(payload, list) else payload["entries"]
        for row in rows:
            raw = row.get("publication_number", "") or row.get("filename", "")
            match = re.match(r"((?:EP|JP|KR)\d+(?:-\d{5,})?(?:[ABC]\d)?)", raw, re.I)
            if not match:
                continue
            publication = normalize_publication(match[1])
            item = candidates.setdefault(
                publication,
                {
                    "publication_number": publication,
                    "jurisdiction": publication[:2],
                    "kind_code_verified": bool(re.search(r"[ABC]\d$", publication)),
                    "discovery_evidence": [],
                    "family_status": "not_resolved_exact_publication_only",
                    "source_role": "patent_document_candidate_not_new_independent_lens",
                },
            )
            item["discovery_evidence"].append(
                {
                    "source_url": row.get("index_url") or row.get("catalog_url"),
                    "source_path": str(path.relative_to(ROOT)),
                    "filename": row.get("filename") or row.get("data_filename"),
                    "title": row.get("title") or row.get("metadata", {}).get("notes", ""),
                }
            )
    for publication, title, url in OFFICIAL_CANDIDATES:
        item = candidates.setdefault(
            publication,
            {
                "publication_number": publication,
                "jurisdiction": "EP",
                "kind_code_verified": True,
                "discovery_evidence": [],
                "family_status": "not_resolved_exact_publication_only",
                "source_role": "patent_document_candidate_not_new_independent_lens",
            },
        )
        item["discovery_evidence"].append({"source_url": url, "title": title})
    eps, queue = [], []
    for publication, item in sorted(candidates.items()):
        if item["jurisdiction"] == "EP":
            if not item["kind_code_verified"]:
                item["requested_publication_number"] = publication + "A1"
                item["kind_selection"] = (
                    "Try A1 at official format index; validate returned XML before acceptance"
                )
                if publication == "EP2664955":
                    # A1 format-index returned HTTP 500; the documented A2
                    # format-index was independently verified to expose XML/PDF.
                    item["requested_publication_number"] = publication + "A2"
                    item["kind_selection"] = "A2 available in verified official format index"
                    item["discovery_evidence"].append(
                        {
                            "source_url": REST + "EP2664955NWA2",
                            "title": "Official A2 format-index verified after A1 was unavailable",
                        }
                    )
            else:
                item["requested_publication_number"] = publication
            eps.append(item)
        else:
            item["status"] = "official_document_not_acquired"
            item["next_step"] = (
                "Search exact number in official public UI; API ingestion requires "
                "service registration/entitlement"
            )
            item["official_search_url"] = (
                "https://www.j-platpat.inpit.go.jp/"
                if item["jurisdiction"] == "JP"
                else "https://www.kipris.or.kr/"
            )
            queue.append(item)
    return eps, queue, inputs


def local_tag(element: ET.Element) -> str:
    return element.tag.split("}")[-1]


def text_content(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def analyze_xml(content: bytes, publication: str) -> tuple[dict, dict]:
    # ElementTree does not load external DTDs. Reject explicit entity declarations
    # as an additional guard while preserving the official XML bytes unchanged.
    if b"<!ENTITY" in content.upper():
        raise ValueError("Unexpected XML entity declaration")
    root = ET.fromstring(content)
    actual = normalize_publication(
        root.get("country", "") + root.get("doc-number", "") + root.get("kind", "")
    )
    if local_tag(root) != "ep-patent-document" or actual != publication:
        raise ValueError(
            f"Publication XML identity mismatch: requested {publication}, got {actual}"
        )
    titles = [text_content(el) for el in root.iter() if local_tag(el) == "B542"]
    applicants = [
        text_content(el)
        for party in root.iter()
        if local_tag(party) == "B711"
        for el in party.iter()
        if local_tag(el) == "snm"
    ]
    proprietors = [
        text_content(el)
        for party in root.iter()
        if local_tag(party) == "B731"
        for el in party.iter()
        if local_tag(el) == "snm"
    ]
    priorities = [
        {"raw_xml": ET.tostring(el, encoding="unicode"), "text": text_content(el)}
        for el in root.iter()
        if local_tag(el) == "B300"
    ]
    tables = []
    for index, table in enumerate((el for el in root.iter() if local_tag(el) == "table"), 1):
        rows = []
        for row in table.iter():
            if local_tag(row) not in {"row", "tr"}:
                continue
            cells = [
                {
                    "tag": local_tag(cell),
                    "attributes": dict(cell.attrib),
                    "text": text_content(cell),
                    "xml": ET.tostring(cell, encoding="unicode"),
                }
                for cell in row
                if local_tag(cell) in {"entry", "td", "th"}
            ]
            if cells:
                rows.append(cells)
        body = text_content(table)
        # A discovery hint, not a prescription classifier or validity check.
        marker = bool(
            re.search(
                r"radius|curvature|abbe|aspher|refract|surface|\bNd\b|\bN[dD][1-9]?\b", body, re.I
            )
        )
        tables.append(
            {
                "table_index": index,
                "attributes": dict(table.attrib),
                "rows": rows,
                "text": body,
                "optical_keyword_hint": marker,
                "xml": ET.tostring(table, encoding="unicode"),
            }
        )
    date = root.get("date-publ", "")
    metadata = {
        "publication_number": actual,
        "publication_date": date,
        "language": root.get("lang"),
        "titles": titles,
        "applicants": applicants,
        "proprietors": proprietors,
        "priority_claims": priorities,
        "family_status": "not_resolved_exact_publication_only",
        "table_count": len(tables),
        "optical_keyword_table_count": sum(table["optical_keyword_hint"] for table in tables),
        "xml_fidelity": "pre_2006_converted_xml_must_check_pdf"
        if date and date < "20060101"
        else "official_xml_numeric_tables_not_yet_pdf_checked",
        "normalization_status": "not_attempted_raw_table_evidence_only",
        "normalized_lens_count": 0,
    }
    extracted = {
        "schema": "camerae2e.official_patent_table_evidence.v1",
        "publication_number": actual,
        "source_sha256": sha256(content),
        "source_format": "epo_xml",
        "status": "unnormalized_unvalidated_table_evidence",
        "note": (
            "Original source XML is preserved. Cell XML retains spans and "
            "mathematical markup; cell text is only a display aid. Tables are not "
            "counted as lens prescriptions."
        ),
        "tables": tables,
    }
    return metadata, extracted


def exact_legacy_publications(path: Path) -> set[str]:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        publications = set()
        for table in tables:
            safe_table = '"' + table.replace('"', '""') + '"'
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({safe_table})")}
            if "publication_number" in columns:
                publications.update(
                    normalize_publication(row[0])
                    for row in connection.execute(
                        f"SELECT DISTINCT publication_number FROM {safe_table}"
                    )
                    if row[0]
                )
        return publications


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "camerae2e_db/sources/nonus_patents")
    parser.add_argument(
        "--report-dir", type=Path, default=ROOT / "reports/lens_source_expansion_v2"
    )
    parser.add_argument("--source-root", type=Path, default=ROOT / "camerae2e_db/sources")
    parser.add_argument(
        "--legacy-db",
        type=Path,
        default=ROOT
        / "camerae2e_db/lens_db/CameraE2E_Lens_DB_v9_20260627"
        / "data/lens_patents/lens_patent_simulation_v9.sqlite",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20 or not 0 <= args.delay <= 60:
        parser.error("limit must be 1..20; delay must be 0..60")
    fetcher = Fetcher(args.output, args.delay, args.offline)
    eps, queue, inputs = build_candidates(args.source_root)
    legacy = exact_legacy_publications(args.legacy_db)
    legacy_before = sha256(args.legacy_db.read_bytes())
    for row in eps + queue:
        pub = row["publication_number"]
        row["present_in_v9_exact_publication"] = (
            pub in legacy or re.sub(r"[ABC]\d$", "", pub) in legacy
        )
        row["legacy_comparison_scope"] = "exact_publication_only_not_family_or_optical_design"
    write_json(
        args.output / "candidates.json",
        {
            "schema": "camerae2e.nonus_patent_candidates.v1",
            "inputs": inputs,
            "ep_candidates": eps,
            "jp_kr_queue": queue,
        },
    )
    services = []
    for evidence in SERVICE_EVIDENCE:
        entry = dict(evidence)
        try:
            _, entry["snapshot"] = fetcher.get(entry["cache_path"], entry["url"])
        except Exception as exc:  # noqa: BLE001
            entry["snapshot_error"] = str(exc)
        services.append(entry)
    documents, bibliographic_records, failures = [], [], []
    for index, candidate in enumerate(eps[: args.limit], 1):
        publication = candidate["requested_publication_number"]
        match = re.fullmatch(r"EP(\d+)([ABC]\d)", publication)
        if not match:
            raise ValueError(f"Invalid selected publication: {publication}")
        eps_id = f"EP{match[1]}NW{match[2]}"
        index_url = REST + eps_id
        print(f"[EP {index}/{min(len(eps), args.limit)}] {publication}", flush=True)
        try:
            data, index_meta = fetcher.get(f"metadata/formats/{publication}.html", index_url)
            links = {
                Path(urllib.parse.urlparse(href).path).suffix.lstrip("."): urllib.parse.urljoin(
                    index_url, html.unescape(href)
                )
                for href in re.findall(r'<a\s+href="([^"]+)"', data.decode("utf-8"))
            }
            if "xml" in links and "pdf" not in links:
                xml_url = links["xml"]
                if xml_url != index_url + "/document.xml":
                    raise ValueError("Unexpected XML source URL")
                xml, xml_meta = fetcher.get(f"raw/{publication}/document.xml", xml_url)
                metadata, _ = analyze_xml(xml, publication)
                bibliographic_records.append(
                    {
                        **metadata,
                        "discovery": candidate,
                        "files": [{**xml_meta, "format": "xml"}],
                        "format_index": index_meta,
                        "status": "xml_only_no_pdf_exposed_in_official_index",
                        "next_step": (
                            "Retrieve the corresponding WO document or a later EP specification"
                        ),
                    }
                )
                print(f"  XML only: no PDF link exposed; {metadata['table_count']} tables")
                continue
            for fmt in ("xml", "pdf"):
                expected_path = f"/publication-server/rest/v1.2/patents/{eps_id}/document.{fmt}"
                if (
                    fmt not in links
                    or urllib.parse.urlparse(links[fmt]).netloc != "data.epo.org"
                    or urllib.parse.urlparse(links[fmt]).path != expected_path
                ):
                    raise ValueError(
                        f"Official format index did not expose expected {fmt} for {publication}"
                    )
            xml, xml_meta = fetcher.get(f"raw/{publication}/document.xml", links["xml"])
            metadata, table_evidence = analyze_xml(xml, publication)
            pdf, pdf_meta = fetcher.get(f"raw/{publication}/document.pdf", links["pdf"])
            if not pdf.startswith(b"%PDF-"):
                raise ValueError("Official PDF response does not have a PDF signature")
            table_path = f"extracted/{publication}.tables.json"
            write_json(args.output / table_path, table_evidence)
            documents.append(
                {
                    **metadata,
                    "discovery": candidate,
                    "status": "official_xml_and_pdf_acquired_tables_archived",
                    "format_index": index_meta,
                    "files": [{**xml_meta, "format": "xml"}, {**pdf_meta, "format": "pdf"}],
                    "table_evidence_path": table_path,
                    "table_evidence_sha256": sha256((args.output / table_path).read_bytes()),
                    "license": {
                        "status": "official_public_document_website_terms_no_open_license_asserted",
                        "terms_url": "https://www.epo.org/en/footer/terms",
                        "patent_rights_status": "not_evaluated",
                    },
                }
            )
            print(
                f"  archived {len(xml) + len(pdf):,} bytes; "
                f"{len(table_evidence['tables'])} raw tables; normalized 0",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {"publication_number": publication, "source_url": index_url, "error": str(exc)}
            )
            print(f"  failed: {exc}", file=sys.stderr, flush=True)
    if sha256(args.legacy_db.read_bytes()) != legacy_before:
        raise RuntimeError("Legacy database changed during acquisition")
    manifest = {
        "schema": "camerae2e.official_patent_documents.v1",
        "source": "EPO European Publication Server",
        "scope": "official_document_archive_not_lens_catalog_import_manifest",
        "documents": documents,
        "bibliographic_records": bibliographic_records,
        "failures": failures,
        "service_access": services,
        "inputs": inputs,
        "legacy_database": {
            "path": str(args.legacy_db.relative_to(ROOT)),
            "sha256": legacy_before,
            "unchanged": True,
        },
        "summary": {
            "selected_ep_publications": min(len(eps), args.limit),
            "acquired_ep_publications": len(documents),
            "acquired_source_files": len(documents) * 2,
            "bibliographic_only_ep_publications": len(bibliographic_records),
            "bibliographic_only_source_files": len(bibliographic_records),
            "downloaded_source_bytes": sum(
                file["bytes"] for doc in documents for file in doc["files"]
            ),
            "raw_xml_table_count": sum(doc["table_count"] for doc in documents),
            "optical_keyword_table_count": sum(
                doc["optical_keyword_table_count"] for doc in documents
            ),
            "normalized_lens_count": 0,
            "verified_new_independent_design_count": 0,
            "verified_patent_family_count": 0,
            "jp_kr_queue": dict(Counter(row["jurisdiction"] for row in queue)),
            "failures": len(failures),
        },
    }
    write_json(args.output / "manifest.json", manifest)
    write_json(args.report_dir / "nonus_patents.json", manifest)
    fieldnames = [
        "publication_number",
        "jurisdiction",
        "present_in_v9_exact_publication",
        "family_status",
        "status",
        "official_search_url",
        "next_step",
        "discovery_evidence",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in queue:
        writer.writerow(
            {**row, "discovery_evidence": json.dumps(row["discovery_evidence"], ensure_ascii=False)}
        )
    args.report_dir.mkdir(parents=True, exist_ok=True)
    (args.report_dir / "nonus_jp_kr_queue.csv").write_text(buf.getvalue())
    lines = [
        "# Non-US official patent source acquisition",
        "",
        f"Acquired **{len(documents)} distinct EP publications / "
        f"{2 * len(documents)} official XML/PDF files**. "
        f"Extracted {manifest['summary']['raw_xml_table_count']} XML tables "
        "as unnormalized evidence. "
        "**0 normalized lenses; 0 verified independent new designs; 0 verified patent families.** "
        f"Separately archived {len(bibliographic_records)} XML-only bibliographic records with "
        "no PDF exposed in the official format index.",
        "",
        (
            "These documents are an official-source verification and extraction "
            "queue. Publication-number differences do not prove a different patent "
            "family or lens design. Source index product names are discovery labels, "
            "not verified production prescriptions."
        ),
        "",
        "| Publication | Applicant / proprietor | Raw tables | Fidelity / next step |",
        "|---|---|---:|---|",
    ]
    for doc in documents:
        parties = doc["applicants"] or doc["proprietors"]
        lines.append(
            f"| [{doc['publication_number']}]({doc['files'][1]['source_url']}) | "
            f"{'; '.join(parties)} | {doc['table_count']} | {doc['xml_fidelity']} |"
        )
    lines += [
        "",
        (
            "All source files carry original/resolved URLs, retrieval timestamps, "
            "sizes and SHA256 sidecars. The v9 database was opened read-only and its "
            "full hash remained unchanged. XML identity was checked against the "
            "requested publication; PDF signatures were checked. Numeric "
            "transcription and optical validation remain separate work."
        ),
        "",
        "## Service access",
        "",
    ]
    for service in services:
        lines.append(f"- [{service['service']}]({service['url']}): {service['detail']}")
    lines += [
        "",
        "JP/KR exact-publication candidates: "
        f"{dict(Counter(row['jurisdiction'] for row in queue))}. "
        "See `nonus_jp_kr_queue.csv`; these are not acquired documents. "
        "No registration, purchase, email, or authenticated API request was made.",
        "",
        "## Reproduce",
        "",
        "```sh",
        "python3 tools/acquire_nonus_patent_sources.py",
        "python3 tools/acquire_nonus_patent_sources.py --offline",
        "```",
        "",
        (
            "Existing raw files are never overwritten. Cached files require matching "
            "SHA256 and URL. No downloaded source content is executed. XML is parsed "
            "without loading external DTDs. Table cell XML, row/column-span "
            "attributes, and mathematical markup are retained to avoid silently "
            "flattening numerical meaning."
        ),
        "",
    ]
    if failures:
        lines += ["## Failed publications", ""] + [
            f"- {row['publication_number']}: {row['error']}" for row in failures
        ]
    (args.report_dir / "nonus_patents.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(manifest["summary"], ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
