"""Immutable E-SFR export ownership, native units and independent lineage."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from camera_platform import calibration_lab, esfr_bridge, esfr_lab
from camera_platform.registry import (
    ConflictError,
    IntegrityError,
    SQLiteRegistry,
    canonical,
)


def metadata(index=0, source_kind="synthetic", cfa="RGGB"):
    return {
        "schema": "camera_esfr_capture_v1",
        "source_kind": source_kind,
        "camera_id": "camera-serial-17",
        "session_id": f"session-{index}",
        "acquisition_id": f"capture-{index}",
        "role": "development" if index == 0 else "validation",
        "pixel_pitch_um": 2.9,
        "conditions": {
            "mode_id": "native-rggb",
            "exposure_s": 0.01,
            "gain": 1.0,
            "temperature_c": 22.5,
            "light_id": "D65",
            "aperture_f_number": 4.12,
            "focus_distance_m": 2.0,
            "focus_setting": "locked",
        },
        "raw_format": {
            "packing": "unpacked16",
            "width": 80,
            "height": 64,
            "stride_bytes": 160,
            "byte_order": "little",
            "bit_depth": 12,
            "cfa": cfa,
            "black_level_dn": 64,
            "white_level_dn": 4095,
        },
        "geometry": {"full_sensor_shape": [100, 120], "crop_origin": [3, 7]},
        "isp": {"transfer": "srgb", "coordinate_mapping": "same_native_raster"},
    }


def fixture_run(tmp_path, *, source_kind="synthetic", change=None, overshoot=False, repeats=1):
    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path / "state"))
    captures = []
    for index in range(repeats * 2):
        m = metadata(index, source_kind)
        m["role"] = "development" if index < repeats else "validation"
        if change:
            change(m)
        raw = tmp_path / f"raw-{index}.bin"
        rng = np.random.default_rng(90 + index)
        raw.write_bytes(rng.integers(100, 4000, (64, 80), dtype=np.uint16).tobytes())
        isp = tmp_path / f"isp-{index}.png"
        Image.fromarray(rng.integers(10, 250, (64, 80, 3), dtype=np.uint8)).save(isp)
        if source_kind == "measured":
            # Historical registry fixture only: measured onboarding is tested
            # separately and never labels these generated raster files measured.
            # Preserve old measured-record export compatibility without bypassing
            # the production importer or requiring a fake physical acquisition.
            imported = esfr_lab.import_capture(service, {**m, "source_kind": "synthetic"}, raw, isp)
            document = {key: value for key, value in esfr_lab.capture(service, imported["id"]).items()
                        if key not in {"id", "created_at", "content_hash"}}
            document["metadata"] = m
            imported = service.registry.put(esfr_lab.CAPTURES, document)
        else:
            imported = esfr_lab.import_capture(service, m, raw, isp)
        captures.append(esfr_lab.capture(service, imported["id"]))
    observations = []
    for index, c in enumerate(captures):
        sfr = [1, 1.1 if overshoot else 0.94, 0.8 - index * 0.01, 0.55, 0.29, 0.1]
        observations.append(
            {
                "capture_id": c["id"],
                "role": c["metadata"]["role"],
                "source_kind": source_kind,
                "rois": [
                    {
                        "id": "edge-a",
                        "kind": "edge",
                        "field_id": "upper-left",
                        "box": [5, 9, 32, 40],
                        "raw": {
                            "Gr": {
                                "status": "ok",
                                "edge_angle_deg": 25.0,
                                "frequency_direction_deg": 115.0,
                                "frequency_cy_native_px": [0, 0.05, 0.1, 0.15, 0.2, 0.25],
                                "sfr": sfr,
                            }
                        },
                    }
                ],
            }
        )
    spec = {
        "chart_profile": {"id": "owned-chart", "reference_status": "unavailable"},
        "baseline_sigma_native_px": 1.5,
        "source": None,
    }
    job = service.registry.submit(
        {
            "job_kind": esfr_lab.KIND,
            "plugin_id": "camera_esfr_validation_v1",
            "name": "Measured chart",
            "specification": spec,
            "capture_snapshots": captures,
            "source_context": None,
        },
        "fixture",
        force=True,
    )
    assert service.registry.claim()["id"] == job["id"]
    summary = {"run_id": job["id"], "observations": observations}
    bound = service.registry.store_blob(
        canonical(summary).encode(), "esfr-result.json", "application/json"
    )
    service.registry.finish(job["id"], result={"summary": summary, "artifacts": [bound]})
    return service, job["id"], captures


@pytest.mark.parametrize("kind", ["synthetic", "measured"])
def test_export_preserves_kind_originals_units_orientation_and_roles(tmp_path, kind):
    service, run_id, captures = fixture_run(tmp_path, source_kind=kind)
    exported = esfr_bridge.export(service, run_id)
    assert exported["source_kind"] == kind
    assert exported["status"] == "ready_for_mtf_study"
    assert len(exported["training_ids"]) == len(exported["validation_ids"]) == 1
    assert exported["study_request"]["measurement_ids"] == exported["training_ids"]
    records = [calibration_lab._measurement(service, x) for x in exported["measurement_ids"]]
    assert [m["role"] for m in records] == ["training", "final_validation"]
    for m, capture in zip(records, captures, strict=True):
        obs = m["observations"]
        assert m["source_kind"] == kind
        assert m["measurement_domain"] == "camera_raw"
        assert obs["orientation_deg"] == 115  # frequency normal, not edge tangent 25
        assert max(obs["frequency_cy_px"]) == 0.25  # no hidden factor of two
        assert obs["field_xy"] == [2 * (7 + 9 + 39 / 2) / 119 - 1, 2 * (3 + 5 + 31 / 2) / 99 - 1]
        assert m["provenance"]["source_metadata"] == capture["metadata"]
        assert capture["files"]["raw"]["sha256"] in m["source_frame_hashes"]
        assert capture["native_pixel_sha256"] in m["source_frame_hashes"]
        assert (
            "acquisition:camera-serial-17:" + capture["metadata"]["acquisition_id"]
            in m["lineage_tokens"]
        )
        for key in ("raw", "isp"):
            bound = service.registry.artifact_binding(
                capture["files"][key]["sha256"],
                owner_kind=calibration_lab.MEASUREMENTS,
                owner_id=m["id"],
            )
            assert (
                bound["path"].read_bytes() == esfr_lab._binding(service, capture, key).read_bytes()
            )
    campaign = service.registry.get(calibration_lab.CAMPAIGNS, exported["campaign_id"])
    assert campaign["native_shape"] == [100, 120]
    assert campaign["baseline_profile"]["esfr_bridge_scope"]["placeholder_defaults_used"]
    assert campaign["baseline_profile"]["physical_validation"] == "not_established"
    assert exported == esfr_bridge.export(service, run_id)
    assert len(service.registry.list(calibration_lab.CAMPAIGNS)) == 1


def test_missing_required_conditions_fail_before_creating_campaign(tmp_path):
    service, run_id, _ = fixture_run(
        tmp_path, change=lambda m: m["conditions"].pop("temperature_c")
    )
    with pytest.raises(ValueError, match="temperature_c"):
        esfr_bridge.export(service, run_id)
    assert not service.registry.list(calibration_lab.CAMPAIGNS)


def test_unsupported_full_sensor_cfa_does_not_get_relabelled(tmp_path):
    service, run_id, _ = fixture_run(tmp_path, change=lambda m: m["raw_format"].update(cfa="BGGR"))
    with pytest.raises(ValueError, match="RGGB only"):
        esfr_bridge.export(service, run_id)
    assert not service.registry.list(calibration_lab.CAMPAIGNS)


def test_independent_roles_cannot_share_a_session(tmp_path):
    service, run_id, _ = fixture_run(tmp_path, change=lambda m: m.update(session_id="same-session"))
    with pytest.raises(ConflictError, match="reuse"):
        esfr_bridge.export(service, run_id)
    assert not service.registry.list(calibration_lab.CAMPAIGNS)


def test_original_artifact_tampering_prevents_export(tmp_path):
    service, run_id, captures = fixture_run(tmp_path)
    path = esfr_lab._binding(service, captures[0], "raw")
    path.write_bytes(b"corrupted-original")
    with pytest.raises(IntegrityError):
        esfr_bridge.export(service, run_id)
    assert not service.registry.list(calibration_lab.CAMPAIGNS)


def test_sfr_above_one_preserved_for_explicit_native_forward_fitter(tmp_path):
    service, run_id, _ = fixture_run(tmp_path, overshoot=True)
    exported = esfr_bridge.export(service, run_id)
    assert exported["status"] == "ready_for_mtf_study"
    assert exported["study_request"]["fit_method"] == "esfr_forward_observer_v1"
    assert exported["sfr_observation_scope"]["overshoot_measurement_count"] == 1
    assert exported["sfr_observation_scope"]["overshoot_preserved"] is True
    record = calibration_lab._measurement(service, exported["training_ids"][0])
    assert max(record["observations"]["mtf"]) == 1.1


def test_explicit_baseline_must_match_declared_conditions_and_depth():
    m = metadata()
    profile = esfr_bridge.baseline_profile(m)
    supplied = copy.deepcopy(profile)
    supplied["conditions"]["gain"] = 2
    m["calibration_bridge"] = {"baseline_profile": supplied}
    with pytest.raises(ValueError, match="conditions"):
        esfr_bridge.baseline_profile(m)
    supplied["conditions"]["gain"] = 1
    supplied["electronics"]["bit_depth"] = 14
    with pytest.raises(ValueError, match="bit depth"):
        esfr_bridge.baseline_profile(m)


def test_repeat_mean_retains_every_original_curve_std_and_lineage(tmp_path):
    from camera_platform.calibration_lab_engine import fit_profile

    service, run_id, captures = fixture_run(tmp_path, repeats=5)
    exported = esfr_bridge.export(service, run_id)
    assert len(exported["training_ids"]) == len(exported["validation_ids"]) == 1
    training = calibration_lab._measurement(service, exported["training_ids"][0])
    provenance = training["provenance"]
    assert provenance["constituent_count"] == 5
    assert provenance["aggregation_method"] == "arithmetic_mean_all_same_role_native_roi_repeats"
    np.testing.assert_allclose(training["observations"]["mtf"][2], 0.78)
    np.testing.assert_allclose(
        provenance["repeat_sample_std_sfr"][2], np.std([0.8, 0.79, 0.78, 0.77, 0.76], ddof=1)
    )
    assert {x["capture_id"] for x in provenance["constituents"]} == {c["id"] for c in captures[:5]}
    for c in captures[:5]:
        assert (
            "session:camera-serial-17:" + c["metadata"]["session_id"] in training["lineage_tokens"]
        )
        service.registry.artifact_binding(
            c["files"]["raw"]["sha256"],
            owner_kind=calibration_lab.MEASUREMENTS,
            owner_id=training["id"],
        )
    individual = esfr_bridge.export(service, run_id, aggregate_repeats=False)
    assert len(individual["training_ids"]) == len(individual["validation_ids"]) == 5
    assert individual["campaign_id"] != exported["campaign_id"]
    campaign = service.registry.get(calibration_lab.CAMPAIGNS, exported["campaign_id"])
    fitted = fit_profile(campaign["baseline_profile"], [training])
    # Actual existing fitter accepts the export and leaves electronic/color placeholders fixed.
    assert fitted["profile"]["electronics"] == campaign["baseline_profile"]["electronics"]
    assert fitted["profile"]["isp"] == campaign["baseline_profile"]["isp"]


def test_curve_rejects_isp_frequency_or_tangent_confusion():
    m = metadata()
    c = {
        "id": "capture",
        "content_hash": "a" * 64,
        "native_pixel_sha256": "b" * 64,
        "files": {"raw": {"sha256": "c" * 64}},
    }
    roi = {
        "id": "edge",
        "box": [0, 0, 20, 20],
        "raw": {
            "Gr": {
                "status": "ok",
                "frequency_cy_native_px": [0, 0.1, 0.5],
                "sfr": [1, 0.8, 0.1],
                "edge_angle_deg": 20,
                "frequency_direction_deg": 110,
            }
        },
    }
    with pytest.raises(IntegrityError, match="0.25"):
        esfr_bridge._curve(m, c, roi, "campaign", "run", {})
    roi["raw"]["Gr"]["frequency_cy_native_px"][-1] = 0.25
    roi["raw"]["Gr"]["frequency_direction_deg"] = 20
    with pytest.raises(IntegrityError, match="direction disagree"):
        esfr_bridge._curve(m, c, roi, "campaign", "run", {})


def mock_db(monkeypatch, service):
    from camera_platform import calibration_db, calibration_db_optics_engine

    source = {
        "run_id": "run-" + "1" * 32,
        "candidate_id": "candidate-03",
        "condition_id": "condition-006",
    }
    context = {**source, "manifest_sha256": "a" * 64}
    row = {"id": source["run_id"], "result": {"summary": {"id": source["run_id"]}}}
    calls = []
    monkeypatch.setattr(
        calibration_db, "source_selection", lambda service, selected: (row, context)
    )

    def convert(*args, **kwargs):
        calls.append((args, kwargs))
        blur = copy.deepcopy(args[3]["blur"])
        kernel = [[0, 0, 0], [0.1, 0.6, 0.1], [0, 0.2, 0]]
        blur.update(family="field_effective_psf_v1", field_application="explicit_nodes_only")
        blur["field_nodes"][0].update(
            family="sampled_native_kernel_v1",
            kernel=kernel,
            kernel_pitch_px=1.0,
            sampling="native_cell_center_pixel_integrated",
        )
        return {
            "can_apply": True,
            "profile_patch": {
                "blur": blur,
                "conditions": {"light_id": "D65", "aperture_f_number": 4.12},
            },
            "source_identity": context,
            "source_files": {"optics.npz": {"sha256": "b" * 64}},
            "report": {"qualification": "sampled_kernel_exact_transfer"},
        }

    monkeypatch.setattr(calibration_db_optics_engine, "convert_source", convert)
    return source, calls


def test_db_baseline_keeps_exact_sampled_kernel_and_center_scope(tmp_path, monkeypatch):
    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path / "state"))
    source, calls = mock_db(monkeypatch, service)
    result = esfr_bridge.db_baseline(service, source, metadata())
    assert result["field_scope"] == "center_only"
    assert calls[0][1]["field_mapping"] is None
    node = result["profile"]["blur"]["field_nodes"][0]
    assert node["family"] == "sampled_native_kernel_v1"
    assert node["kernel"] == [[0, 0, 0], [0.1, 0.6, 0.1], [0, 0.2, 0]]
    assert result["evidence_kind"] == "model_derived_not_measured"
    assert result["source_context"]["run_id"] == source["run_id"]
    assert not service.registry.list(calibration_lab.CAMPAIGNS)


def test_db_baseline_rejects_condition_mismatch_and_non_native_pitch(tmp_path, monkeypatch):
    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path / "state"))
    source, _ = mock_db(monkeypatch, service)
    m = metadata()
    m["conditions"]["light_id"] = "A"
    with pytest.raises(ValueError, match="light_id"):
        esfr_bridge.db_baseline(service, source, m)
    m = metadata()
    m["pixel_pitch_um"] = 3.0
    with pytest.raises(ValueError, match="2.9"):
        esfr_bridge.db_baseline(service, source, m)
