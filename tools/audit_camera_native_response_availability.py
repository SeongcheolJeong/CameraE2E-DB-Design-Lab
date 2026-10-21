"""Read-only, reproducible source inventory for p0014 native angular coupling.

Usage: PYTHONPATH=src .venv-platform/bin/python \
  tools/audit_camera_native_response_availability.py --output /path/to/report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QE_FIELDS = {
    "measurement_data_status", "quantitative_points", "qualitative_mentions",
    "spectral_ranges", "filter_sets",
}
POINT_FIELDS = {
    "wavelength_nm", "qe_percent", "source_kind", "confidence",
    "method", "source_file", "context",
}
QE_MODELS = "camerae2e_db/fdtd_tcad/image_sensor_db/optical_qe_db/models"
FDTD_RESPONSES = "reports/fdtd_sensor_e2e/solver_v3/responses"
PROXY_LUT = (
    "camerae2e_db/fdtd_tcad/runs/"
    "convergence_cra3_rgb_r84_gridsnap_quant/camera_lut.json"
)
NATIVE_FIXTURE = (
    "reports/camera_platform/native-pixels-v016/artifacts/"
    "b1bef618c23e1c4c0070ad6907ee8a6719410f9b5be81a42c64b6c0d546fc766/"
    "native_pixel_response.json"
)
PSF_RESULT = (
    "reports/camera_platform/integrated-workflows-20260928/"
    "dev29-p0014-restricted-band/verification.json"
)
DIRECTION_RESULT = (
    "reports/camera_platform/integrated-workflows-20260928/"
    "dev30-planar-tail-directional-final/result.json"
)


def _read(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text())


def _sha(relative: str) -> str:
    digest = hashlib.sha256()
    with (ROOT / relative).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def audit() -> dict:
    qe_root = ROOT / QE_MODELS
    files = sorted(qe_root.glob("*.json"))
    statuses = Counter()
    quantitative = []
    observed_qe_fields = set()
    observed_point_fields = set()
    for path in files:
        payload = json.loads(path.read_text())
        qe = payload.get("qe", {})
        observed_qe_fields.update(qe)
        if not set(qe) <= QE_FIELDS:
            raise ValueError(f"QE schema changed; review native-grid check: {path}")
        for point in qe.get("quantitative_points", []):
            observed_point_fields.update(point)
            if not set(point) <= POINT_FIELDS:
                raise ValueError(f"QE point schema changed; review native-grid check: {path}")
        status = qe.get("measurement_data_status", "missing")
        statuses[status] += 1
        if status == "reported_qe_points":
            quantitative.append({
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": _sha(path.relative_to(ROOT).as_posix()),
                "point_count": len(qe.get("quantitative_points", [])),
                "wavelength_nm": [p.get("wavelength_nm")
                                  for p in qe.get("quantitative_points", [])],
                "point_extraction_methods": sorted({p.get("method")
                                                    for p in qe.get("quantitative_points", [])}),
                "has_native_theta_phi_site_grid": False,
            })
    fdtd = []
    for path in sorted((ROOT / FDTD_RESPONSES).glob("*_unverified.json")):
        relative = path.relative_to(ROOT).as_posix()
        data = _read(relative)
        gates = data.get("validation", {}).get("gates", {})
        fdtd.append({
            "path": relative, "sha256": _sha(relative),
            "sensor_id": data.get("sensor_id"),
            "pixel_pitch_um": data.get("pixel_pitch_um"),
            "expected_sample_count": data.get("expected_sample_count"),
            "completed_sample_count": data.get("completed_sample_count"),
            "validation_status": data.get("validation", {}).get("status"),
            "numerical_gates": {k: gates.get(k) for k in
                                ("energy_balance", "spatial", "time", "pml", "interpolation")},
            "normalization": data.get("provenance", {}).get("normalization"),
            "eligible_for_independent_site_collected_eqe": False,
        })
    proxy = _read(PROXY_LUT)
    fixture = _read(NATIVE_FIXTURE)
    psf = _read(PSF_RESULT)
    direction = _read(DIRECTION_RESULT)["model_candidate"]["directional_response"]
    angles = []
    for field_index, rays in enumerate(direction["directions_xyz"]):
        for ray_index, ray in enumerate(rays):
            if any(wave[field_index][ray_index] > 0
                   for wave in direction["conditional_weights"]):
                angles.append(math.degrees(math.acos(max(-1.0, min(1.0, ray[2])))))
    lens = {
        "psf_result": {"path": PSF_RESULT, "sha256": _sha(PSF_RESULT),
                       "status": psf["status"],
                       "pixel_pitch_um": psf["spatial_support"]["pixel_pitch_um"]},
        "direction_result": {"path": DIRECTION_RESULT, "sha256": _sha(DIRECTION_RESULT),
                             "wavelength_nm": direction["wavelength_nm"],
                             "active_transmitted_ray_max_theta_deg": max(angles),
                             "reference_plane": direction["reference_plane"],
                             "direction_convention": direction["direction_convention"]},
    }
    return {
        "schema": "camera_native_response_availability_audit_v1",
        "scope": "Listed local QE, FDTD and authored-fixture assets; no private asset inventory",
        "count_method": {
            "qe_models": f"sorted glob {QE_MODELS}/*.json; inspect qe.measurement_data_status",
            "fdtd_responses": f"sorted glob {FDTD_RESPONSES}/*_unverified.json",
            "lens_max_theta": "max acos(z) over any positive-power saved joint ray",
        },
        "lens": lens,
        "qe_models": {
            "directory": QE_MODELS,
            "model_json_count": len(files),
            "qe_fields_observed": sorted(observed_qe_fields),
            "quantitative_point_fields_observed": sorted(observed_point_fields),
            "measurement_data_status_counts": dict(sorted(statuses.items())),
            "reported_qe_points": quantitative,
            "native_grid_count_method": (
                "All qe and quantitative_points keys are checked against the "
                "scalar/catalog field sets in this script; unknown keys stop the audit"
            ),
            "native_wavelength_theta_phi_site_grid_count": 0,
        },
        "fdtd_native_candidates": fdtd,
        "legacy_proxy_lut": {
            "path": PROXY_LUT, "sha256": _sha(PROXY_LUT),
            "schema": proxy["schema"], "pixel_pitch_um": proxy["geometry"]["pitch"],
            "wavelength_nm": proxy["wavelengths_nm"],
            "cell_pixels": proxy["cell_pixels"],
            "sensor_stack_name": proxy["sensor_stack"]["name"],
            "eligible_for_independent_site_collected_eqe": False,
        },
        "authored_native_fixture": {
            "path": NATIVE_FIXTURE, "sha256": _sha(NATIVE_FIXTURE),
            "pixel_pitch_um": fixture["pixel_pitch_um"],
            "wavelength_nm": fixture["wavelength_nm"],
            "theta_deg": fixture["theta_deg"],
            "source": fixture["source"],
            "eligible_for_p0014_550_650nm": False,
        },
        "decision": {
            "qualified_native_response_found": False,
            "p0014_joint_native_photoelectron_rate_or_raw": "REFUSED",
            "reason_codes": [
                "qe_catalog_lacks_native_angular_site_grid",
                "sensor_specific_fdtd_production_luts_empty_and_unverified",
                "legacy_fdtd_lut_proxy_pitch_and_normalization_incompatible",
                "authored_fixture_650nm_out_of_domain",
            ],
            "physical_validation": "not_established",
            "product_ranking_allowed": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
