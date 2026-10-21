"""HTTP import to immutable report, scoped application and fixed-profile replay."""

import hashlib
import json
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import numpy as np
import pytest
import tifffile
from camera_platform.esfr_engine import bilinear_isp
from camera_platform.esfr_lab import ACTIVATIONS, CAPTURES, PROFILES, ROOT
from fastapi.testclient import TestClient
from scipy.special import erf

from camera_platform.api import create_app
from camera_platform.registry import digest


def _synthetic_metadata(index, shape):
    """Declare generated pixels explicitly; never inherit a measured blank form."""
    role = "development" if index < 2 else "validation"
    return {
        "schema": "camera_esfr_capture_v1",
        "camera_id": "lifecycle-synthetic-camera", "source_kind": "synthetic",
        "acquisition_id": f"frame-{index}", "session_id": f"{role}-session",
        "role": role, "pixel_pitch_um": 2.9,
        "synthetic_generator": {"method": "independent_gaussian_cdf_fixture", "seed": 900 + index},
        "raw_format": {
            "packing": "unpacked16", "width": shape[1], "height": shape[0],
            "stride_bytes": shape[1] * 2, "header_bytes": 0, "byte_order": "little",
            "bit_depth": 12, "bit_shift": 0, "cfa": "RGGB",
            "black_level_dn": 64, "white_level_dn": 4095,
        },
        "geometry": {"full_sensor_shape": list(shape), "crop_origin": [0, 0]},
        "isp": {"transfer": "linear", "coordinate_mapping": "same_native_raster"},
        "conditions": {
            "exposure_s": 0.01, "gain": 1.0, "light_id": "synthetic-light",
            "focus_setting": "synthetic-fixed", "distance_m": 2.0,
            "mode_id": "linear-bayer", "temperature_c": 25,
            "aperture_f_number": 4.12, "focus_distance_m": 2.0,
        },
    }


def _upload_pair(client, headers, index, *, isp_override=None):
    """Independent Gaussian CDF fixture; this is software evidence, not a chart."""
    shape = (176, 336)
    metadata = _synthetic_metadata(index, shape)
    levels = {
        channel: {"low_dn": 600, "high_dn": 2800, "black_dn": 64, "white_dn": 4095}
        for channel in ("R", "Gr", "Gb", "B")
    }
    raw = np.full(shape, 1500.0)
    yy, xx = np.indices((128, 128))
    for i, angle in enumerate((5, 85)):
        theta = np.deg2rad(angle)
        distance = -np.sin(theta) * (xx - 63.3) + np.cos(theta) * (yy - 63.7)
        edge = 64 + 600 + 2200 * (0.5 + 0.5 * erf(distance / (np.sqrt(2) * 1.25)))
        raw[24:152, 16 + i * 160 : 144 + i * 160] = edge
    raw = np.uint16(np.rint(raw + np.random.default_rng(900 + index).normal(0, 1, shape)))
    rgb = np.uint16(np.clip(bilinear_isp(raw, "RGGB", (0, 0), levels), 0, 1) * 65535)
    image = BytesIO()
    tifffile.imwrite(image, rgb, photometric="rgb")
    raw_bytes = raw.astype("<u2").tobytes()
    isp_bytes = image.getvalue() if isp_override is None else isp_override
    response = client.post(
        ROOT + "/captures",
        headers=headers,
        data={"metadata": json.dumps(metadata)},
        files={
            "raw": (f"original-frame-{index}.bin", raw_bytes),
            "isp": (f"original-frame-{index}.tiff", isp_bytes),
        },
    )
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["files"]["raw"]["sha256"] == hashlib.sha256(raw_bytes).hexdigest()
    assert record["files"]["isp"]["sha256"] == hashlib.sha256(isp_bytes).hexdigest()
    for domain, suffix, original in (("raw", "bin", raw_bytes), ("isp", "tiff", isp_bytes)):
        filename = f"original-frame-{index}.{suffix}"
        assert record["files"][domain]["name"] == filename
        downloaded = client.get(ROOT + f"/captures/{record['id']}/original/{domain}")
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == original
        assert downloaded.headers["content-disposition"] == f'attachment; filename="{filename}"'
    return record


def _complete(client, service, identity):
    job = service.registry.claim()
    assert job["id"] == identity
    result = service.execute_run(job)
    service.registry.finish(identity, result=result)
    response = client.get(ROOT + "/studies/" + identity)
    assert response.status_code == 200, response.text
    completed = response.json()
    assert completed["status"] == "succeeded"
    return completed["result"]["summary"]


def _verified_files(client, identity, summary, captures):
    """Check both exact owner downloads and the portable ZIP against its manifest."""
    prefix = ROOT + "/studies/" + identity + "/files/"
    blobs = {}
    for relative, checksum in summary["files"].items():
        response = client.get(prefix + relative)
        assert response.status_code == 200, (relative, response.text)
        assert hashlib.sha256(response.content).hexdigest() == checksum
        blobs[relative] = response.content
    manifest = json.loads(blobs["manifest.json"])
    assert manifest["schema"] == "camera_esfr_files_v1"
    expected = manifest["files"]
    assert "report.html" in expected
    assert "result.json" in expected
    assert "candidate-profile.json" in expected
    with ZipFile(BytesIO(blobs["evidence.zip"])) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(expected) | {"manifest.json"}
        assert archive.read("manifest.json") == blobs["manifest.json"]
        for relative, checksum in expected.items():
            archived = archive.read(relative)
            assert hashlib.sha256(archived).hexdigest() == checksum
            assert archived == blobs[relative]
    for capture in captures:
        for domain in ("raw", "isp"):
            assert capture["files"][domain]["sha256"] in expected.values()
    assert "data:image/png;base64," in blobs["report.html"].decode()
    assert json.loads(blobs["candidate-profile.json"]) == summary["candidate_profile"]
    return blobs


def test_complete_report_apply_cas_and_exact_frozen_replay(tmp_path, monkeypatch):
    app = create_app(
        state_dir=tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False
    )
    with TestClient(app) as client:
        service = app.state.service
        headers = {"X-Platform-Token": client.get("/api/v1/session").json()["token"]}
        captures = [_upload_pair(client, headers, i) for i in range(4)]
        submitted = client.post(
            ROOT + "/studies",
            headers=headers,
            json={
                "name": "Complete synthetic E-SFR lifecycle",
                "development_ids": [x["id"] for x in captures[:2]],
                "validation_ids": [x["id"] for x in captures[2:]],
                "source": None,
                "fit": True,
                "baseline_sigma_native_px": 2.4,
                "thresholds": {"raw_sfr_rmse": 0.02, "isp_sfr_rmse": 0.02},
                "rois": [
                    {
                        "id": f"edge-{i}",
                        "kind": "edge",
                        "field_id": "center",
                        "box": [24, 16 + i * 160, 128, 128],
                    }
                    for i in range(2)
                ],
            },
        )
        assert submitted.status_code == 202, submitted.text
        identity = submitted.json()["id"]
        summary = _complete(client, service, identity)
        assert summary["source_kind"] == "synthetic"
        assert summary["physical_validation"] == "not_established"
        assert summary["fit"]["candidate_frozen_before_holdout"] is True
        assert summary["validation"]["status"] == "pass"
        for domain in ("raw", "isp"):
            metric = summary["metrics"][domain + "_sfr_rmse"]
            assert metric["after"] < 0.02
            assert metric["after"] < metric["before"] * 0.15
        fitted_files = _verified_files(client, identity, summary, captures)
        assert service.registry.list(PROFILES) == []
        assert service.registry.list(ACTIVATIONS) == []

        # The caller must acknowledge the exact active revision, including an empty slot.
        apply_url = ROOT + "/studies/" + identity + "/apply"
        assert client.post(apply_url, headers=headers, json={}).status_code == 409
        applied = client.post(apply_url, headers=headers, json={"expected_active_id": None})
        assert applied.status_code == 200, applied.text
        profile = applied.json()
        assert profile["camera_profile"] == summary["candidate_profile"]
        assert profile["profile_sha256"] == digest(summary["candidate_profile"])
        assert profile["scope"] == "chart_conditioned_effective_response"
        assert profile["activation"]["scope"] == "esfr_only_no_prescription_overwrite"
        active = client.get(
            ROOT + "/active", params={"camera_id": "lifecycle-synthetic-camera"}
        ).json()
        assert active["profile_id"] == profile["id"]
        assert client.get(ROOT + "/active", params={"camera_id": "different-camera"}).json() is None
        assert client.post(
            apply_url, headers=headers, json={"expected_active_id": None}
        ).status_code == 409
        assert len(service.registry.list(PROFILES)) == 1

        replay_url = ROOT + "/studies/" + identity + "/replay-applied"
        assert client.post(
            replay_url, headers=headers, json={"profile_id": "not-active"}
        ).status_code == 409
        response = client.post(replay_url, headers=headers, json={"profile_id": profile["id"]})
        assert response.status_code == 202, response.text
        replay_id = response.json()["id"]
        assert replay_id != identity

        import camera_platform.esfr_engine as engine

        def cannot_fit(*args, **kwargs):
            raise AssertionError("Applied-profile replay must not refit or rederive geometry")

        monkeypatch.setattr(engine, "minimize_scalar", cannot_fit)
        monkeypatch.setattr(engine, "minimize", cannot_fit)
        monkeypatch.setattr(engine, "_geometry", cannot_fit)
        replay = _complete(client, service, replay_id)
        replay_files = _verified_files(client, replay_id, replay, captures)
        assert replay["fit"]["status"] == "frozen_replay"
        assert replay["replay_of_applied_profile"] == profile["id"]
        assert replay["candidate_profile"] == summary["candidate_profile"]
        assert replay["candidate_profile_sha256"] == summary["candidate_profile_sha256"]
        assert replay["metrics"] == summary["metrics"]
        for comparison in summary["comparisons"]:
            for key in ("raw_array", "isp_array"):
                relative = comparison.get("images", {}).get("after", {}).get(key)
                if relative:
                    assert replay_files[relative] == fitted_files[relative]
        # A replay is new execution evidence, not a new fit or a new physical acquisition.
        assert len(service.registry.list(CAPTURES)) == 4
        assert len(service.registry.list(PROFILES)) == 1
        assert len(service.registry.list(ACTIVATIONS)) == 1
        assert client.get(ROOT + "/studies/" + identity).json()["status"] == "succeeded"


@pytest.mark.parametrize("reencode", [False, True], ids=["same-file", "same-pixels-new-tiff"])
def test_reused_isp_cannot_masquerade_as_independent_validation(tmp_path, reencode):
    app = create_app(
        state_dir=tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False
    )
    with TestClient(app) as client:
        service = app.state.service
        headers = {"X-Platform-Token": client.get("/api/v1/session").json()["token"]}
        dev = _upload_pair(client, headers, 0)
        original = client.get(ROOT + f"/captures/{dev['id']}/original/isp").content
        if reencode:
            changed = BytesIO()
            tifffile.imwrite(
                changed,
                tifffile.imread(BytesIO(original)),
                photometric="rgb",
                description="Changed TIFF metadata is not a new physical capture",
            )
            repeated = changed.getvalue()
            assert hashlib.sha256(repeated).digest() != hashlib.sha256(original).digest()
        else:
            repeated = original
        val = _upload_pair(client, headers, 2, isp_override=repeated)
        assert dev["native_pixel_sha256"] != val["native_pixel_sha256"]
        assert dev["files"]["raw"]["sha256"] != val["files"]["raw"]["sha256"]
        assert dev["isp_pixel_sha256"] == val["isp_pixel_sha256"]
        if reencode:
            assert dev["files"]["isp"]["sha256"] != val["files"]["isp"]["sha256"]
        response = client.post(
            ROOT + "/studies",
            headers=headers,
            json={
                "development_ids": [dev["id"]],
                "validation_ids": [val["id"]],
                "rois": [{"id": "edge", "kind": "edge", "box": [24, 16, 128, 128]}],
            },
        )
        assert response.status_code == 422, response.text
        assert "Independent validation overlaps" in response.json()["error"]
        assert service.registry.list_runs() == []
        assert service.registry.list(PROFILES) == []
        assert service.registry.list(ACTIVATIONS) == []


@pytest.mark.parametrize(
    ("rois", "message"),
    [
        (
            [
                {"id": f"gray-{i}", "kind": "gray", "box": [0, 0, 1024, 1024]}
                for i in range(8)
            ],
            "Total native ROI area exceeds 8 megapixels",
        ),
        ([{"id": "edge", "kind": "edge", "box": [0, 0, 513, 512]}], "262144"),
    ],
    ids=["summed-patch-area", "single-edge-area"],
)
def test_roi_memory_budget_rejected_at_submission_and_worker_entry(
    tmp_path, monkeypatch, rois, message
):
    import camera_platform.esfr_engine as engine
    import camera_platform.esfr_lab as lab

    from camera_platform.calibration_lab import _source_code

    # Boundedness is checked from trusted capture metadata before file I/O or a queue write.
    metadata = _synthetic_metadata(0, (1024, 1024))
    capture = {
        "id": "development-capture",
        "metadata": metadata,
        "raw_shape": [1024, 1024],
        "native_pixel_sha256": "raw-pixels",
        "isp_pixel_sha256": "isp-pixels",
        "files": {"raw": {"sha256": "raw-file"}, "isp": {"sha256": "isp-file"}},
    }
    mutations = []

    def unexpected_access(*args, **kwargs):
        mutations.append((args, kwargs))
        raise AssertionError("Over-budget input reached file I/O or the queue")

    service = SimpleNamespace(
        loaded_environment=_source_code(), registry=SimpleNamespace(submit=unexpected_access)
    )
    monkeypatch.setattr(lab, "capture", lambda service, identity: capture)
    monkeypatch.setattr(engine, "RawFrame", unexpected_access)
    monkeypatch.setattr(engine, "read_isp", unexpected_access)
    with pytest.raises(ValueError, match=message):
        lab.submit(
            service,
            {"development_ids": [capture["id"]], "validation_ids": [], "rois": rois},
        )
    with pytest.raises(ValueError, match=message):
        engine.run(
            {
                "development_ids": [capture["id"]],
                "validation_ids": [],
                "specification": {"rois": rois},
            },
            [(capture, tmp_path / "must-not-read.raw", tmp_path / "must-not-read.tiff")],
            tmp_path / "out",
        )
    assert mutations == []


@pytest.mark.parametrize("validation_status", ["fail", "inconclusive"])
def test_nonpassing_validation_cannot_activate_or_write_records(monkeypatch, validation_status):
    import camera_platform.esfr_lab as lab

    mutations = []

    def unexpected_write(*args, **kwargs):
        mutations.append((args, kwargs))
        raise AssertionError("A nonpassing candidate reached activation state")

    row = {
        "status": "succeeded",
        "result": {
            "summary": {
                "candidate_profile": {
                    "camera_id": "camera",
                    "fields": {"center": {"sigma_x_px": 1.25, "sigma_y_px": 1.25}},
                },
                "fit": {"status": "fitted"},
                "validation": {"status": validation_status},
            }
        },
    }
    service = SimpleNamespace(registry=SimpleNamespace(put=unexpected_write))
    monkeypatch.setattr(lab, "get", lambda service, identity: row)
    monkeypatch.setattr(lab, "active", unexpected_write)
    with pytest.raises(ValueError, match="Independent validation must pass explicit thresholds"):
        lab.apply(service, "fitted-study", {"expected_active_id": None})
    assert mutations == []
