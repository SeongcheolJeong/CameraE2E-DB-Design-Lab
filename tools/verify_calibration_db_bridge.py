"""Verify explicit electrical calibration against a frozen DB candidate.

Creates a new, clearly synthetic campaign; restores its baseline after verification.
The original DB run, optical rates and camera campaigns are never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import numpy as np

from camera_platform.calibration_lab_engine import default_profile, synthetic_fixture
from camera_platform.sdk import PlatformClient


def progress(message):
    print(message, file=sys.stderr, flush=True)


def verify(base_url, source_run, candidate, output, *, condition=None):
    with PlatformClient(base_url, timeout=120) as client:
        progress("Verifying the exact DB source and preserving the electrical bridge")
        source_before = client.calibration_lab_template(
            source_run=source_run, candidate=candidate, condition=condition
        )["source_context"]
        bridge = client.create_calibration_db_bridge(source_run, candidate, condition=condition)
        projection = bridge["projection"]
        baseline = default_profile()
        baseline["electronics"] = deepcopy(projection["electronics"])
        baseline["pixel_pitch_um"] = projection["pixel_pitch_um"]
        baseline["cfa"] = projection["cfa"]
        baseline["conditions"].update(projection["conditions"])
        # This is an explicit synthetic probe, not an inferred physical sensor size.
        baseline["native_shape"] = [128, 128]
        baseline["render_shape"] = [128, 128]
        baseline["reference_white_e"] = min(4000.0, baseline["electronics"]["full_well_e"] / 2)
        source = {"run_id": source_run, "candidate_id": candidate}
        if condition is not None:
            source["condition_id"] = condition
        campaign = client.create_calibration_campaign(
            {
                "name": "DB 전기 연결 · 합성 데이터 구현 검증",
                "camera_id": "synthetic-db-electrical-" + uuid.uuid4().hex[:12],
                "native_shape": baseline["native_shape"],
                "pixel_pitch_um": baseline["pixel_pitch_um"],
                "conditions": baseline["conditions"],
                "baseline_profile": baseline,
                "thresholds": {"dark": 1e-6, "ptc": 1e-5},
                "source": source,
                "db_bridge_id": bridge["id"],
            }
        )
        progress("Synthetic campaign: " + campaign["id"])
        fixture = synthetic_fixture()
        expected = {
            **baseline["electronics"],
            "black_level_dn": 72.0,
            "dn_per_e": baseline["electronics"]["dn_per_e"] * 1.15,
            "read_noise_e": 4.0,
            "dark_current_e_per_s": 6.0,
        }
        measurements = []
        for original in fixture["measurements"]:
            if original["chart"] not in {"dark", "ptc"}:
                continue
            row = {k: deepcopy(v) for k, v in original.items() if k != "id"}
            row.update(
                campaign_id=campaign["id"],
                physical_instance=campaign["camera_id"],
                conditions=deepcopy(baseline["conditions"]),
            )
            k, rn, black, dark = (
                expected[n]
                for n in ("dn_per_e", "read_noise_e", "black_level_dn", "dark_current_e_per_s")
            )
            if row["chart"] == "dark":
                t = np.asarray(row["observations"]["exposure_s"])
                row["observations"] = {
                    "exposure_s": t.tolist(),
                    "mean_dn": (black + t * dark * k).tolist(),
                    "variance_dn2": ((rn * k) ** 2 + t * dark * k * k + 1 / 12).tolist(),
                }
            else:
                signal = np.asarray(row["observations"]["mean_dn"]) - 72
                row["observations"] = {
                    "mean_dn": (black + signal).tolist(),
                    "variance_dn2": (k * signal + (rn * k) ** 2 + 1 / 12).tolist(),
                }
            measurements.append(client.import_calibration_measurement(row))
        study = client.submit_calibration_study(
            {
                "campaign_id": campaign["id"],
                "base_profile_id": "baseline",
                "measurement_ids": [m["id"] for m in measurements if m["role"] == "training"],
                "name": "DB 전기 기준 → Dark/PTC 후보",
                "seed": 7,
                "max_iterations": 100,
            }
        )
        progress("Fitting electrical parameters: " + study["id"])
        fitted = client.wait(study["id"], timeout=1860, expected_kind="calibration_lab")
        assert fitted["status"] == "succeeded", fitted.get("error")
        recovered = fitted["result"]["summary"]["candidate_profile"]["electronics"]
        for name in ("black_level_dn", "dn_per_e", "read_noise_e", "dark_current_e_per_s"):
            assert np.isclose(recovered[name], expected[name], rtol=1e-7, atol=1e-7), name
        final = client.validate_calibration_study(
            study["id"], [m["id"] for m in measurements if m["role"] == "final_validation"]
        )
        progress("Validating independent observations: " + final["id"])
        final = client.wait(final["id"], timeout=1860, expected_kind="calibration_lab_validation")
        assert final["status"] == "succeeded", final.get("error")
        assert final["result"]["summary"]["validation"]["status"] == "pass"
        selected = client.apply_calibration_study(study["id"], campaign["id"])
        try:
            replay = client.submit_calibration_db_replay(
                campaign["id"], selected["profile"]["id"], seed=7
            )
            progress("Generating paired DB RAW and ISP outputs: " + replay["id"])
            replay = client.wait_calibration_db_replay(replay["id"])
            assert replay["status"] == "succeeded", replay.get("error")
            summary = replay["result"]["summary"]
            assert summary["physical_validation"] == "not_established"
            assert summary["parameter_scope"] == "electronics_only"
            assert summary["fresh_simulation"] is True
            assert summary["source_raw_modified"] is False
            assert summary["optics_recomputed"] is False

            def check(item):
                relative, expected_hash = item
                response = client.http.get(summary["files_base"] + relative)
                response.raise_for_status()
                assert hashlib.sha256(response.content).hexdigest() == expected_hash, relative
                if relative.endswith(".npy") and "/raw-" in relative:
                    raw = np.load(io.BytesIO(response.content), allow_pickle=False)
                    assert raw.shape == (256, 256) and raw.dtype.kind in "iu", relative
                return relative, expected_hash

            total = len(summary["files"])
            progress(f"Checking {total} downloaded output hashes")
            verified = {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                for count, (relative, checksum) in enumerate(
                    pool.map(check, summary["files"].items()), start=1
                ):
                    verified[relative] = checksum
                    if count % 64 == 0 or count == total:
                        progress(f"Verified {count}/{total} output files")
            source_after = client.calibration_lab_template(
                source_run=source_run, candidate=candidate, condition=condition
            )["source_context"]
            assert source_after == source_before
        finally:
            client.activate_calibration_profile(campaign["id"], None)
            progress("Restored the synthetic campaign baseline")
        assert client.calibration_campaign(campaign["id"])["active_profile"] is None
        query = f"campaign={campaign['id']}&study={study['id']}&replay={replay['id']}"
        query += f"&source_run={source_run}&candidate={candidate}"
        if condition is not None:
            query += "&condition=" + condition
        report = {
            "schema": "calibration_db_live_verification_v1",
            "verified_at_unix": time.time(),
            "evidence_kind": "synthetic_implementation_verification",
            "physical_validation": "not_established",
            "source": source,
            "source_unchanged": True,
            "bridge_id": bridge["id"],
            "campaign_id": campaign["id"],
            "study_id": study["id"],
            "validation_id": final["id"],
            "replay_id": replay["id"],
            "profile_id": selected["profile"]["id"],
            "restored_baseline": True,
            "expected_electronics": expected,
            "recovered_electronics": recovered,
            "applied_parameters": summary["applied_parameters"],
            "source_files": summary["source_files"],
            "verified_files": verified,
            "source_code": summary["source_code"],
            "url": base_url.rstrip("/") + "/calibration-lab.html?" + query,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return {
            "report": str(output.resolve()),
            "url": report["url"],
            "replay_id": replay["id"],
            "verified_files": len(verified),
            "physical_validation": "not_established",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--condition")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            verify(
                args.base_url,
                args.source_run,
                args.candidate,
                args.output,
                condition=args.condition,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
