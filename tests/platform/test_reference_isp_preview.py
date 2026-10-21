"""Single-frame native RAW preview never starts a new camera acquisition."""

import copy
import json
import shutil

import numpy as np
import pytest

from camera_platform import reference_isp
from camera_platform import reference_workshop_engine as engine
from camera_platform import reference_workshop_job as job
from camera_platform.design_workshop_engine import write_json
from camera_platform.virtual_lab_job import sha256


@pytest.fixture(scope="module")
def preview_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("isp-preview-source")
    spec = job.specification({"luminance_cd_m2": 1000, "isp": reference_isp.default_profile()})
    readout = {
        "gain": 1,
        "full_well_e": 10000,
        "read_noise_e": 2,
        "dark_current_e_per_s": 1,
        "bit_depth": 12,
        "black_level_dn": 64,
    }
    yy, xx = np.indices((256, 256))
    raw = (200 + 80 * np.sin(xx / 3) + yy / 4).astype(np.uint16)
    rates = {"center": np.full((256, 256), 10000.0), "outer-ne": np.full((256, 256), 5000.0)}
    calibration = {
        "identity": "fixture-fixed-calibration",
        "baseline_only": True,
        "color": {"matrix": (np.eye(3) / 1000).tolist()},
        "lsc": engine.pixel.build_lsc(rates),
    }
    write_json(root / "calibration.json", calibration)
    cell = {
        "source_report": "fixture",
        "pixel_pitch_um": 2.9,
        "cross_section": {},
        "provenance": {"fixture": True},
        "readout": readout,
    }
    write_json(root / "pixel-cell.json", cell)
    write_json(root / "pixel-response.json", {"fixture": True})
    captures, fields = {}, []
    for identity in rates:
        geometry = {"native_shape_row_col": [256, 256], "local_cfa_origin_row_col": [0, 0]}
        fields.append({"id": identity, "status": "succeeded", "geometry": geometry})
        records = {}
        for chart in ("texture", "uniform", "demosaic_chirp"):
            folder = root / "fields" / identity / chart
            folder.mkdir(parents=True)
            records[chart] = {"raw": []}
            for index in range(8):
                path = folder / f"raw-{index:02d}.npy"
                np.save(path, raw + index)
                records[chart]["raw"].append(
                    {"file": path.name, "sha256": sha256(path), "seed": index}
                )
            for name, array, key in (
                ("mean-electron-rate.npy", rates[identity], "rate_sha256"),
                (
                    "source-reference-linear.npy",
                    np.full((256, 256, 3), 0.1),
                    "source_reference_sha256",
                ),
            ):
                np.save(folder / name, array)
                records[chart][key] = sha256(folder / name)
            if chart == "demosaic_chirp":
                np.save(
                    folder / "planes-electron-rate.npy",
                    np.repeat(rates[identity][..., None], 4, axis=-1),
                )
                records[chart]["planes_sha256"] = sha256(folder / "planes-electron-rate.npy")
        captures[identity] = {
            "configuration": spec,
            "geometry": geometry,
            "readout": readout,
            "calibration": calibration["color"],
            "captures": records,
        }
    write_json(root / "optical-response.json", {"revision": "fixture-optics", "fields": fields})
    write_json(
        root / "capture.json",
        {
            "configuration": spec,
            "fields": captures,
            "calibration_sha256": sha256(root / "calibration.json"),
        },
    )
    return root, spec


def _hashes(root):
    return {p.relative_to(root).as_posix(): sha256(p) for p in root.rglob("*") if p.is_file()}


def test_preview_processes_exactly_one_existing_frame_and_preserves_source(
    preview_source, tmp_path, monkeypatch
):
    source, spec = preview_source
    spec = copy.deepcopy(spec)
    spec["isp"]["lsc_enabled"] = True
    spec["isp"]["raw_denoise"]["strength"] = 0.4
    before = _hashes(source)
    monkeypatch.setattr(engine, "_solution", lambda *_: pytest.fail("Preview solved optics"))
    monkeypatch.setattr(
        engine.shared, "_capture", lambda *_args, **_kwargs: pytest.fail("Preview acquired RAW")
    )
    original_process = reference_isp.process
    calls = []

    def process(*args, **kwargs):
        calls.append(args[0].copy())
        return original_process(*args, **kwargs)

    monkeypatch.setattr(reference_isp, "process", process)
    output = tmp_path / "preview"
    result = engine.preview(source, output, spec, field_id="outer-ne", chart="texture")
    assert len(calls) == 1
    expected_raw = np.load(source / "fields/outer-ne/texture/raw-00.npy")
    np.testing.assert_array_equal(calls[0], expected_raw)
    assert _hashes(source) == before
    assert result["raw_preserved"] and result["acquisition_reused"]
    assert result["preview_scope"]["processed_frame_count"] == 1
    assert result["preview_scope"]["candidate_selection"] == "not_evaluated"
    assert result["successful_fields"] == 1 and result["failed_fields"] == 0
    item = result["fields"][0]
    assert item["id"] == "outer-ne" and set(item["images"]) == {"texture"}
    assert item["metrics"]["processed_temporal_noise"] is None
    assert len(list(output.glob("fields/*/*/raw-*.npy"))) == 1
    assert sha256(output / item["images"]["texture"]["raw"]) == result["provenance"]["raw_sha256"]
    assert (output / "fields/outer-ne/texture/isp-stages/manifest.json").is_file()
    matrix = json.loads((source / "calibration.json").read_text())["color"]["matrix"]
    readout = json.loads((source / "pixel-cell.json").read_text())["readout"]
    expected = original_process(
        expected_raw, readout, matrix, spec["isp"], lsc_gain=np.full((256, 256), 2.0)
    )
    actual = np.load(output / "fields/outer-ne/texture/processed.npy")
    np.testing.assert_allclose(actual, expected[1], rtol=1e-6)
    engine.report(result, output)
    assert (output / "report.html").is_file()


@pytest.mark.parametrize(
    "name",
    [
        "raw-00.npy",
        "mean-electron-rate.npy",
        "source-reference-linear.npy",
        "planes-electron-rate.npy",
    ],
)
def test_preview_rejects_changed_selected_inputs(preview_source, tmp_path, name):
    source = tmp_path / "source"
    shutil.copytree(preview_source[0], source)
    path = source / "fields/center/demosaic_chirp" / name
    values = np.load(path)
    values.flat[0] += 1
    np.save(path, values)
    with pytest.raises(ValueError, match="Preserved preview artifact changed"):
        engine.preview(
            source,
            tmp_path / "preview",
            preview_source[1],
            field_id="center",
            chart="demosaic_chirp",
        )


def test_preview_rejects_missing_chart_acquisition_change_and_nested_source(
    preview_source, tmp_path
):
    source, spec = preview_source
    with pytest.raises(ValueError, match="chart is absent"):
        engine.preview(source, tmp_path / "missing", spec, field_id="center", chart="color")
    changed = copy.deepcopy(spec)
    changed["gain"] = 2
    with pytest.raises(ValueError, match="only ISP"):
        engine.preview(source, tmp_path / "changed", changed, field_id="center", chart="texture")
    with pytest.raises(ValueError, match="disjoint"):
        engine.preview(source, source / "nested", spec, field_id="center", chart="texture")


def test_preview_cannot_be_used_as_complete_acquisition(preview_source, tmp_path):
    source, spec = preview_source
    output = tmp_path / "preview"
    engine.preview(source, output, spec, field_id="center", chart="uniform")
    with pytest.raises(ValueError, match="complete preserved acquisition"):
        engine.preview(
            output, tmp_path / "nested-preview", spec, field_id="center", chart="uniform"
        )
    with pytest.raises(ValueError, match="complete preserved acquisition"):
        engine.replay(output, tmp_path / "replay", spec)


def test_preview_color_binding_is_checked_before_processing(preview_source, tmp_path, monkeypatch):
    from camera_platform import reference_isp_color

    source, spec = preview_source
    spec = copy.deepcopy(spec)
    spec["isp"]["color_calibration_id"] = "fixed-recalibration"
    calibration = {"id": "fixed-recalibration", "matrix": (np.eye(3) / 500).tolist()}
    validated = []

    def validate(value, folder):
        assert value is calibration and folder == source.resolve()
        validated.append(True)

    monkeypatch.setattr(reference_isp_color, "validate_binding", validate)
    original_process = reference_isp.process

    def process(*args, **kwargs):
        assert validated == [True]
        np.testing.assert_array_equal(args[2], calibration["matrix"])
        return original_process(*args, **kwargs)

    monkeypatch.setattr(reference_isp, "process", process)
    output = tmp_path / "color-preview"
    result = engine.preview(
        source, output, spec, field_id="center", chart="uniform", color_calibration=calibration
    )
    assert result["calibration"]["color"] == calibration
    assert (
        json.loads((output / "color-calibrations/fixed-recalibration.json").read_text())
        == calibration
    )


def test_preview_rejects_modified_baseline_calibration(preview_source, tmp_path):
    source = tmp_path / "source"
    shutil.copytree(preview_source[0], source)
    path = source / "calibration.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="baseline calibration changed"):
        engine.preview(
            source, tmp_path / "preview", preview_source[1], field_id="center", chart="uniform"
        )
