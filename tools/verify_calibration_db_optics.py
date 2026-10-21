"""Exercise DB optical initialization, synthetic MTF fitting and fresh chart renders.

Creates a separate implementation-verification campaign and restores its baseline.
Does not establish measured camera accuracy or alter the original DB source.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import uuid
from copy import deepcopy
from pathlib import Path

import numpy as np

from camera_platform.calibration_lab_engine import _mtf, default_profile, synthetic_fixture
from camera_platform.sdk import PlatformClient


def progress(message):
    print(message, file=sys.stderr, flush=True)


def verify(
    base_url,
    source_run,
    candidate,
    output,
    *,
    condition=None,
    bridge_id=None,
    field_mapping_mode=None,
):
    with PlatformClient(base_url, timeout=180) as client:
        progress("Preserving DB optical initialization with its exact source")
        bridge = (
            client.calibration_db_bridge(bridge_id)
            if bridge_id
            else client.create_calibration_db_bridge(source_run, candidate, condition=condition)
        )
        source = {"run_id": source_run, "candidate_id": candidate}
        if condition is not None:
            source["condition_id"] = condition
        for key, value in source.items():
            assert bridge["source_context"][key] == value
        progress("Electrical source compatibility verified")
        profile = default_profile()
        for key in ("electronics", "pixel_pitch_um", "cfa"):
            profile[key] = deepcopy(bridge["projection"][key])
        profile["conditions"].update(bridge["projection"]["conditions"])
        original = deepcopy(profile)
        conversion = client.create_calibration_db_optical_profile(
            source_run,
            candidate,
            profile,
            condition=condition,
            field_mapping_mode=field_mapping_mode,
        )
        assert profile == original
        assert conversion["can_apply"] is True, conversion["report"]
        assert conversion["status"] == "qualified"
        progress("Optical conversion: " + conversion["id"])
        assert conversion["physical_validation"] == "not_established"
        assert (
            conversion["source_context"]["manifest_sha256"]
            == bridge["source_context"]["manifest_sha256"]
        )
        assert any(name.endswith("optics.npz") for name in conversion["source_files"])
        profile["blur"] = deepcopy(conversion["profile_patch"]["blur"])
        profile["conditions"].update(conversion["profile_patch"].get("conditions", {}))
        saved = client.calibration_db_optical_profile(conversion["id"])
        assert saved == conversion
        downloaded = {}
        for name, descriptor in conversion["output_files"].items():
            response = client.http.get(conversion["files_base"] + name)
            response.raise_for_status()
            assert hashlib.sha256(response.content).hexdigest() == descriptor["sha256"]
            downloaded["conversion/" + name] = descriptor["sha256"]
        progress("Conversion outputs downloaded and hash verified")
        campaign = client.create_calibration_campaign(
            {
                "name": (
                    "DB 중심·주변부 자동 변환 · 합성 MTF 구현 검증"
                    if field_mapping_mode
                    else "DB 광학 자동 변환 · 합성 MTF 구현 검증"
                ),
                "camera_id": "synthetic-db-optics-" + uuid.uuid4().hex[:12],
                "native_shape": profile["native_shape"],
                "pixel_pitch_um": profile["pixel_pitch_um"],
                "conditions": profile["conditions"],
                "baseline_profile": profile,
                "thresholds": {"mtf": 1e-5},
                "source": source,
                "db_bridge_id": bridge["id"],
                "db_optical_profile_id": conversion["id"],
            }
        )
        detail = client.calibration_campaign(campaign["id"])
        assert detail["db_optical_profile"]["id"] == conversion["id"]
        assert detail["active_profile"] is None
        # Distinct synthetic responses at every retained node; none is a physical measurement.
        truths = [
            {
                "field_xy": deepcopy(node["field_xy"]),
                "source_field_id": node["source_field_id"],
                "sigma_x_px": 0.85 + i * 0.15,
                "sigma_y_px": 1.15 + i * 0.25,
                "theta_deg": 18.0 + i * 13.0,
            }
            for i, node in enumerate(profile["blur"]["field_nodes"])
        ]
        domain = profile["blur"].get("field_coordinate_domain", "sensor_normalized")
        if field_mapping_mode == "reference_model":
            assert len(truths) >= 2 and any(n["field_xy"] != [0, 0] for n in truths)
            assert domain == "reference_lens_object_direction"
            assert all(f["applied"] for f in conversion["report"]["fields"])
            assert all(
                max(f["residuals"].values()) <= 1e-12 for f in conversion["report"]["fields"]
            )
        fixture = next(r for r in synthetic_fixture()["measurements"] if r["chart"] == "mtf")
        measurements = []
        for role, count in (("training", 33), ("final_validation", 27)):
            frequency = np.linspace(0, 0.5, count)
            for truth, angle in ((t, a) for t in truths for a in (0, 45, 90, 135)):
                row = {k: deepcopy(v) for k, v in fixture.items() if k != "id"}
                identity = uuid.uuid4().hex
                row.update(
                    campaign_id=campaign["id"],
                    physical_instance=campaign["camera_id"],
                    conditions=deepcopy(profile["conditions"]),
                    role=role,
                    session_id="optical-verification-" + role,
                    acquisition_id=identity,
                    observations={
                        "frequency_cy_px": frequency.tolist(),
                        "mtf": _mtf(truth, frequency, angle).tolist(),
                        "field_xy": truth["field_xy"],
                        "field_coordinate_domain": domain,
                        "orientation_deg": angle,
                    },
                )
                measurements.append(client.import_calibration_measurement(row))
        study = client.submit_calibration_study(
            {
                "campaign_id": campaign["id"],
                "base_profile_id": "baseline",
                "measurement_ids": [m["id"] for m in measurements if m["role"] == "training"],
                "name": "DB field PSF priors → independent per-field synthetic MTF update",
                "seed": 42,
                "max_iterations": 150,
            }
        )
        progress("Fitting and freshly rendering optical chart comparison: " + study["id"])
        fitted = client.wait(study["id"], timeout=300, expected_kind="calibration_lab")
        assert fitted["status"] == "succeeded", fitted.get("error")
        summary = fitted["result"]["summary"]
        recovered = summary["candidate_profile"]["blur"]["field_nodes"]
        for truth in truths:
            node = next(n for n in recovered if n["field_xy"] == truth["field_xy"])
            for angle in (0, 45, 90, 135):
                np.testing.assert_allclose(
                    _mtf(node, frequency, angle), _mtf(truth, frequency, angle), atol=1e-6
                )
        validation = client.validate_calibration_study(
            study["id"], [m["id"] for m in measurements if m["role"] == "final_validation"]
        )
        final = client.wait(
            validation["id"], timeout=300, expected_kind="calibration_lab_validation"
        )
        assert final["status"] == "succeeded", final.get("error")
        assert final["result"]["summary"]["validation"]["status"] == "pass"
        selected = client.apply_calibration_study(study["id"], campaign["id"])
        retained_arrays = {}
        try:
            for name, checksum in summary["files"].items():
                response = client.http.get(summary["files_base"] + name)
                response.raise_for_status()
                assert hashlib.sha256(response.content).hexdigest() == checksum, name
                downloaded["study/" + name] = checksum
                if name.endswith("/effective-kernel.npy") or name.endswith("/raw.npy"):
                    retained_arrays[name] = np.load(
                        io.BytesIO(response.content), allow_pickle=False
                    )
        finally:
            client.activate_calibration_profile(campaign["id"], None)
        render_evidence = []
        images = summary["images"]
        assert len(images) == len(truths) * 8
        for image in (item for item in images if item["variant"] == "baseline"):
            field = image["blur_field"]
            node = next(
                n for n in profile["blur"]["field_nodes"] if n["field_xy"] == field["field_xy"]
            )
            assert field["field_coordinate_domain"] == domain
            assert field["selection"] == "declared_observation"
            kernel_path = str(Path(image["raw"]).parent / "effective-kernel.npy")
            np.testing.assert_allclose(
                retained_arrays[kernel_path], node["kernel"], rtol=0, atol=1e-15
            )
            candidate_image = next(
                item
                for item in images
                if item["measurement_id"] == image["measurement_id"]
                and item["variant"] == "candidate"
            )
            assert candidate_image["blur_field"]["field_xy"] == field["field_xy"]
            assert candidate_image["blur_field"]["field_coordinate_domain"] == domain
            changed = not np.array_equal(
                retained_arrays[image["raw"]], retained_arrays[candidate_image["raw"]]
            )
            assert changed, "Fresh calibrated RAW unexpectedly equals the original model"
            render_evidence.append(
                {
                    "measurement_id": image["measurement_id"],
                    "field": field,
                    "source_kernel_preserved": True,
                    "raw_changed": changed,
                    "baseline_isp": image["isp"],
                    "candidate_isp": candidate_image["isp"],
                }
            )
        assert client.calibration_campaign(campaign["id"])["active_profile"] is None
        after = client.calibration_lab_template(
            source_run=source_run, candidate=candidate, condition=condition
        )["source_context"]
        for key in (
            "run_id",
            "candidate_id",
            "condition_id",
            "manifest_sha256",
            "candidate_sha256",
        ):
            assert after.get(key) == conversion["source_context"].get(key)
        query = f"campaign={campaign['id']}&study={study['id']}"
        report = {
            "schema": "calibration_db_optics_live_verification_v1",
            "verified_at_unix": time.time(),
            "evidence_kind": "synthetic_implementation_verification",
            "physical_validation": "not_established",
            "source": source,
            "conversion_id": conversion["id"],
            "bridge_id": bridge["id"],
            "campaign_id": campaign["id"],
            "study_id": study["id"],
            "validation_id": final["id"],
            "profile_id": selected["profile"]["id"],
            "source_unchanged": True,
            "restored_baseline": True,
            "conversion_report": conversion["report"],
            "source_files": conversion["source_files"],
            "field_coordinate_domain": domain,
            "field_mapping_mode": field_mapping_mode,
            "expected_mtf_nodes": truths,
            "recovered_mtf_nodes": recovered,
            "render_evidence": render_evidence,
            "verified_files": downloaded,
            "source_code": summary["source_code"],
            "url": base_url.rstrip("/") + "/calibration-lab.html?" + query,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return {
            "report": str(output.resolve()),
            "url": report["url"],
            "conversion_id": conversion["id"],
            "verified_files": len(downloaded),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--condition")
    parser.add_argument("--bridge-id")
    parser.add_argument("--field-mapping-mode", choices=["reference_model"])
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
                bridge_id=args.bridge_id,
                field_mapping_mode=args.field_mapping_mode,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
