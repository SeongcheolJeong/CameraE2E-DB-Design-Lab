"""Source-bound electrical projection and actual fresh RAW/Reference ISP replay."""

import hashlib
import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import calibration_db_engine as engine
from camera_platform import calibration_lab_engine, electrical, reference_isp
from camera_platform import design_workshop_engine as shared
from camera_platform.virtual_lab_job import sha256


def write_source(root, *, condition_id=None):
    root.mkdir()
    identity = "candidate-00"
    prefix = (
        f"conditions/{condition_id}/candidates/{identity}/"
        if condition_id
        else f"candidates/{identity}/"
    )
    folder = root / prefix
    folder.mkdir(parents=True)
    readout = {
        "gain": 1,
        "full_well_e": 10000,
        "read_noise_e": 10,
        "dark_current_e_per_s": 10,
        "bit_depth": 12,
        "black_level_dn": 64,
    }
    cell = {
        "readout": readout,
        "electrical_model": electrical.default_electrical_model(readout),
        "pixel_pitch_um": 2.9,
        "cfa_pattern": "RGGB",
        "cell_sha256": "source-cell",
    }
    isp = reference_isp.default_profile()
    isp["demosaic_method"] = "bilinear"
    spec = {
        "name": "authored source",
        "frames": 8,
        "gain": 1,
        "exposure_ms": 4,
        "luminance_cd_m2": 1000,
        "seed": 7,
        "chart_suite": "classic_v1",
        "isp": isp,
        "lens": {"optical_profile": "geometric_retrace"},
    }
    calibration = {
        "color": {"matrix": (np.eye(3) / 2000).tolist()},
        "lsc": None,
        "identity": "frozen-ccm",
    }
    geometry = {
        "native_shape_row_col": [256, 256],
        "pixel_pitch_um": 2.9,
        "cfa_origin": {"pattern": "RGGB", "local_row": 0, "local_col": 0},
    }
    fields = [
        {"id": name, "label": name, "status": "succeeded", "geometry": geometry}
        for name in engine.FIELDS
    ]
    sources = {"lens": {"source": {"pupil_condition": {"effective_f_number": 4.12}}}}
    candidate = {
        "id": identity,
        "status": "succeeded",
        "prefix": prefix,
        "label": "Synthetic fixed DB source",
        "source_identities": sources,
        "parameter_sources": [
            {
                "parameter": key,
                "classification": "assumption",
                "origin": "authored_fixture",
                "source": "independent test fixture",
                "value": value,
            }
            for key, value in readout.items()
        ],
        "result": {"configuration": spec, "pixel": {"cell": cell}, "limitations": []},
    }
    rng = np.random.default_rng(115)
    patches = rng.uniform(0.05, 0.65, (24, 3))
    yy, xx = np.indices((256, 256))
    sites = np.array([[0, 1], [2, 3]])[yy % 2, xx % 2]
    for fi, field_id in enumerate(engine.FIELDS):
        field_root = folder / "fields" / field_id
        field_root.mkdir(parents=True)
        records = {}
        for chart in engine.CHARTS:
            chart_root = field_root / chart
            chart_root.mkdir()
            pattern = shared._patterns(chart, (256, 256))
            rgb = patches[pattern] if chart == "color" else np.repeat(pattern[..., None], 3, 2)
            planes = rgb[..., [0, 1, 1, 2]] * 400000
            rate = np.take_along_axis(planes, sites[..., None], axis=2)[..., 0]
            reference = rgb * 0.8
            record = {}
            for filename, values, key in (
                ("mean-electron-rate.npy", rate, "rate_sha256"),
                ("planes-electron-rate.npy", planes, "planes_sha256"),
                (
                    "source-reference-linear.npy",
                    reference.astype(np.float32),
                    "source_reference_sha256",
                ),
            ):
                np.save(chart_root / filename, values, allow_pickle=False)
                record[key] = sha256(chart_root / filename)
            # An independently invoked source acquisition is the baseline parity oracle.
            captured = electrical.capture_electrons(
                rate * 0.004,
                0.004,
                cell["electrical_model"],
                analog_gain=1,
                temperature_c=25,
                geometry={"full_sensor_shape": [256, 256], "crop_origin": [0, 0]},
                seed=7 + fi * 32,
                noise_enabled=True,
            )
            np.save(chart_root / "raw-00.npy", captured["arrays"]["raw_dn"], allow_pickle=False)
            record["raw"] = [{"file": "raw-00.npy", "sha256": sha256(chart_root / "raw-00.npy")}]
            records[chart] = record
        capture = {
            "configuration": spec,
            "geometry": geometry,
            "readout": readout,
            "calibration": calibration["color"],
            "captures": records,
            "scene_reference": {
                "reference_xyz": (patches * 0.8).tolist(),
                "white_xyz": [0.95, 1, 1.09],
                "asset": {"kind": "authored synthetic fixture"},
            },
        }
        shared.write_json(field_root / "capture.json", capture)
    for name, value in (
        ("pixel-cell", cell),
        ("pixel-response", {}),
        ("calibration", calibration),
        ("optical-response", {"fields": fields}),
        ("result", candidate),
        ("sources", sources),
    ):
        shared.write_json(folder / (name + ".json"), value)
    summary = {
        "schema": "camera_db_design_study_result_v1",
        "status": "succeeded",
        "id": "source-run",
        "comparison_mode": "aperture_conditions" if condition_id else "lens_sensor",
        "candidates": [candidate],
    }
    if condition_id:
        condition = {
            "id": condition_id,
            "luminance_cd_m2": 1000,
            "exposure_ms": 4,
            "candidates": [candidate],
        }
        summary["aperture_conditions"] = {"conditions": [condition]}
        shared.write_json(root / "conditions" / condition_id / "result.json", condition)
    shared.write_json(root / "result.json", summary)
    manifest = {
        "schema": "camera_reference_workshop_files_v1",
        "files": {
            p.relative_to(root).as_posix(): sha256(p) for p in root.rglob("*") if p.is_file()
        },
    }
    shared.write_json(root / "manifest.json", manifest)
    return summary


def profile_for(payload):
    projection = engine.baseline_projection(payload)
    profile = calibration_lab_engine.default_profile()
    profile["electronics"] = projection["electronics"]
    profile["pixel_pitch_um"] = projection["pixel_pitch_um"]
    profile["conditions"].update(projection["conditions"])
    return profile


def test_projection_maps_actual_voltage_adc_transfer_and_preserves_source(tmp_path):
    source = tmp_path / "source"
    summary = write_source(source)
    before = deepcopy(summary)
    payload = engine.prepare_source(
        source, summary, "candidate-00", expected_manifest_sha256=sha256(source / "manifest.json")
    )
    projection = engine.baseline_projection(payload)
    assert projection["electronics"]["dn_per_e"] == pytest.approx(0.4031)
    assert projection["electronics"]["full_well_e"] == 10000
    assert projection["electronics"]["full_well_identified"] is False
    assert projection["probe_shape"] == [256, 256]
    assert "native_shape" not in projection
    assert projection["applied_f_number"] == 4.12
    mapping = {row["target"]: row for row in projection["parameter_mapping"]}
    assert mapping["electronics.read_noise_e"]["classification"] == "assumption"
    assert mapping["electronics.read_noise_e"]["origin"] == "authored_fixture"
    assert (
        mapping["electronics.dn_per_e"]["classification"] == "derived_from_saved_electrical_model"
    )
    assert {r["parameter"] for r in mapping["electronics.dn_per_e"]["source_evidence"]} == {
        "gain",
        "full_well_e",
        "bit_depth",
        "black_level_dn",
    }
    assert set(projection["electronics"]) == set(
        calibration_lab_engine.default_profile()["electronics"]
    )
    assert summary == before
    for name, data in payload["frozen_files"].items():
        assert hashlib.sha256(data).hexdigest() == payload["source_files"][name]["sha256"]
    copy = tmp_path / "snapshot"
    for name, data in payload["frozen_files"].items():
        path = copy / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert (
        engine.baseline_projection(engine.prepare_source(copy, summary, "candidate-00"))
        == projection
    )
    with pytest.raises(ValueError, match="manifest SHA256 pin"):
        engine.prepare_source(copy, None, "candidate-00")
    manifest_only = engine.prepare_source(
        copy, None, "candidate-00", expected_manifest_sha256=sha256(copy / "manifest.json")
    )
    assert engine.baseline_projection(manifest_only) == projection
    assert manifest_only["source_identity"]["run_id"] == "source-run"


def test_condition_grid_requires_exact_nested_condition(tmp_path):
    source = tmp_path / "source"
    summary = write_source(source, condition_id="condition-003")
    with pytest.raises(ValueError, match="explicit condition_id"):
        engine.prepare_source(source, summary, "candidate-00")
    with pytest.raises(ValueError, match="Unknown or ambiguous"):
        engine.prepare_source(source, summary, "candidate-00", "condition-000")
    payload = engine.prepare_source(source, summary, "candidate-00", "condition-003")
    assert payload["source_identity"]["condition_id"] == "condition-003"
    assert "conditions/condition-003/result.json" in payload["frozen_files"]


def test_changed_bound_rate_and_changed_manifest_are_refused(tmp_path):
    source = tmp_path / "source"
    summary = write_source(source)
    with pytest.raises(ValueError, match="manifest changed"):
        engine.prepare_source(source, summary, "candidate-00", expected_manifest_sha256="0" * 64)
    path = source / "candidates/candidate-00/fields/center/uniform/mean-electron-rate.npy"
    values = np.load(path)
    values[0, 0] += 1
    np.save(path, values)
    with pytest.raises(ValueError, match="hash changed"):
        engine.prepare_source(source, summary, "candidate-00")


@pytest.mark.parametrize(
    "field,value", [("dn_per_e", 0), ("dn_per_e", True), ("dn_per_e", float("nan"))]
)
def test_reference_isp_refuses_invalid_explicit_conversion(field, value):
    readout = {
        "gain": 1,
        "full_well_e": 10000,
        "read_noise_e": 2,
        "dark_current_e_per_s": 0,
        "bit_depth": 12,
        "black_level_dn": 64,
        field: value,
    }
    with pytest.raises(ValueError, match="DN/e"):
        reference_isp.readout_dn_per_e(readout)


def test_reference_isp_explicit_gain_controls_electrons_and_noise_hint():
    readout = {
        "gain": 1,
        "full_well_e": 10000,
        "read_noise_e": 2,
        "dark_current_e_per_s": 0,
        "bit_depth": 12,
        "black_level_dn": 64,
        "dn_per_e": 0.8,
    }
    profile = reference_isp.default_profile()
    profile["demosaic_method"] = "bilinear"
    result = reference_isp.process(
        np.full((32, 32), 864, np.uint16), readout, np.eye(3) / 1000, profile, return_stages=True
    )
    np.testing.assert_allclose(result["stages"]["demosaiced_electrons"][4:-4, 4:-4], 1000)
    np.testing.assert_allclose(result["noise_hint_dn2"], 0.8 * 800 + (0.8 * 2) ** 2 + 1 / 12)
    assert readout["full_well_e"] == 10000


def test_replay_reacquires_same_native_rates_with_independent_dn_per_e(tmp_path):
    source = tmp_path / "source"
    summary = write_source(source)
    payload = engine.prepare_source(source, summary, "candidate-00")
    profile = profile_for(payload)
    profile["electronics"].update(
        dn_per_e=0.65, black_level_dn=72, read_noise_e=4, dark_current_e_per_s=20
    )
    profile["blur"]["field_nodes"][0]["sigma_x_px"] = 3
    source_hashes = {p.relative_to(source): sha256(p) for p in source.rglob("*") if p.is_file()}
    output = tmp_path / "replay"
    result = engine.render_replay(payload, profile, output, 7)
    assert result["capture_count"] == 128
    assert len(result["images"]) == 16
    assert len(list(output.rglob("raw-*.npy"))) == 128
    for field in engine.FIELDS:
        for chart in engine.CHARTS:
            original = source / "candidates/candidate-00/fields" / field / chart
            baseline = output / "baseline/fields" / field / chart
            calibrated = output / "calibrated/fields" / field / chart
            np.testing.assert_array_equal(
                np.load(original / "raw-00.npy"), np.load(baseline / "raw-00.npy")
            )
            assert not np.array_equal(
                np.load(baseline / "raw-00.npy"), np.load(calibrated / "raw-00.npy")
            )
            assert sha256(original / "mean-electron-rate.npy") == sha256(
                calibrated / "mean-electron-rate.npy"
            )
            assert (calibrated / "display.png").is_file()
    after = result["variants"][1]
    assert after["readout"]["dn_per_e"] == pytest.approx(0.65)
    assert after["applied_electrical_model"]["full_well_e"] == 10000
    for field in after["fields"]:
        assert field["metrics"]["temporal_noise_e"] == pytest.approx(
            field["metrics"]["temporal_noise_dn"] / 0.65
        )
        assert field["tests"]["noise"]["uniform"]["raw_green"][
            "dark_signal_subtracted_dn"
        ] == pytest.approx(20 * 0.004 * 0.65)
    assert "blur" in {r["parameter"] for r in result["not_transferred"]}
    assert result["physical_validation"] == "not_established"
    assert source_hashes == {
        p.relative_to(source): sha256(p) for p in source.rglob("*") if p.is_file()
    }
    assert json.loads((output / "comparison.json").read_text())["scope"] == "electronics_only"
    with pytest.raises(ValueError, match="new or empty"):
        engine.render_replay(payload, profile, output, 7)


@pytest.mark.parametrize(
    "mutation", ["full_well_e", "bit_depth", "gain", "temperature_c", "pixel_pitch_um", "cfa"]
)
def test_replay_refuses_nontransferable_electrical_geometry_conditions(tmp_path, mutation):
    source = tmp_path / "source"
    summary = write_source(source)
    payload = engine.prepare_source(source, summary, "candidate-00")
    profile = profile_for(payload)
    if mutation in {"full_well_e", "bit_depth"}:
        profile["electronics"][mutation] += 1
    elif mutation in {"gain", "temperature_c"}:
        profile["conditions"][mutation] += 1
    else:
        profile[mutation] = "BGGR" if mutation == "cfa" else 3
    with pytest.raises(ValueError):
        engine.render_replay(payload, profile, tmp_path / "rejected", 7)
    assert not (tmp_path / "rejected").exists()


def test_projection_refuses_fpn_and_postgain_approximations(tmp_path):
    source = tmp_path / "source"
    summary = write_source(source)
    payload = engine.prepare_source(source, summary, "candidate-00")
    for field, value in (("digital_gain", 2), ("post_gain_noise_v", 0.001)):
        broken = deepcopy(payload)
        broken["cell"]["electrical_model"][field] = value
        with pytest.raises(ValueError, match="digital gain"):
            engine.baseline_projection(broken)
    payload["cell"]["electrical_model"]["fixed_pattern"]["prnu_sigma"] = 0.01
    with pytest.raises(ValueError, match="nonzero FPN"):
        engine.baseline_projection(payload)
