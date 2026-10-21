"""Object radiance remains separate from the platform image-plane irradiance."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from camera_platform.lens_sensor_radiance_scene import (
    get_radiance_scene,
    import_radiance_scene,
    load_radiance_scene,
    scene_from_radiance,
    source_identity,
    template,
)
from camera_platform.lens_sensor_raw_job import (
    RADIANCE_SCENE_SCHEMA,
    default_radiance_scene_spec,
    specification,
)
from camera_platform.offline_assets import offline_asset_store
from camera_platform.registry import IntegrityError, SQLiteRegistry
from pyisetcam.scene import scene_get

WORKSPACE = Path(__file__).resolve().parents[2]


def _npy(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


def _metadata() -> dict:
    value = template()
    value.update(
        source_uri="user-supplied://independent-spectral-radiance-run-7",
        license="Owned synthetic test input",
        method_and_version="analytic-gradient-v1",
        wavelength_nm=[400, 450, 550, 650, 700],
    )
    return value


def _array() -> np.ndarray:
    row, col = np.indices((32, 32), dtype=np.float32)
    spatial = (1.0 + row / 64 + col / 32)[..., None]
    spectrum = np.array([0.005, 0.007, 0.011, 0.013, 0.017], dtype=np.float32)
    return np.ascontiguousarray(spatial * spectrum)


def test_import_preserves_original_bytes_and_converts_source_energy_once(tmp_path):
    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path / "registry"))
    metadata, input_array = _metadata(), _array()
    content = _npy(input_array)
    record = import_radiance_scene(service, metadata, content)
    repeated = import_radiance_scene(service, metadata, content)
    assert repeated == record
    assert record["source_sha256"] == record["artifacts"][0]["sha256"]
    assert record["unit"] == "W/m^2/sr/nm"
    assert record["reference_plane"] == "object_directional_radiance_before_lens"
    assert record["physical_validation"] == "not_established"
    assert service.registry.blob_path(record["source_sha256"]).read_bytes() == content
    loaded, radiance = load_radiance_scene(service, record["id"])
    assert loaded == record
    assert np.array_equal(radiance, input_array)
    assert source_identity(loaded)["source_sha256"] == record["source_sha256"]

    assets = offline_asset_store(workspace=WORKSPACE)
    with assets.bound_default(assets):
        scene = scene_from_radiance(record, radiance, assets=assets)
        energy = scene_get(scene, "energy")
    assert scene.fields["distance_m"] == np.inf
    assert scene.fields["fov_deg"] == metadata["horizontal_fov_deg"]
    assert np.asarray(scene.fields["wave"]).tolist() == [450, 550, 650]
    assert np.allclose(energy, input_array[..., [1, 2, 3]], rtol=1e-12, atol=1e-12)
    assert scene.metadata["lens_sensor_radiance_scene"] == source_identity(record)


def test_import_rejects_image_plane_provenance_placeholder_and_bad_spectrum(tmp_path):
    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path / "registry"))
    content = _npy(_array())
    with pytest.raises(ValueError, match="placeholder"):
        import_radiance_scene(service, template(), content)
    metadata = _metadata()
    metadata["reference_plane"] = "sensor_ideal_image_before_psf_ri"
    with pytest.raises(ValueError, match="reference plane"):
        import_radiance_scene(service, metadata, content)
    metadata = _metadata()
    metadata["included_effects"] = ["on_axis_lens_throughput"]
    with pytest.raises(ValueError, match="lens, sensor or ISP"):
        import_radiance_scene(service, metadata, content)
    metadata = _metadata()
    metadata["wavelength_nm"] = [400, 430, 470, 500, 600]
    with pytest.raises(ValueError, match="cover 450, 550 and 650"):
        import_radiance_scene(service, metadata, content)
    with pytest.raises(ValueError, match="nonnegative"):
        negative = _array()
        negative[0, 0, 0] = -1
        import_radiance_scene(service, _metadata(), _npy(negative))
    assert service.registry.list("lens_sensor_radiance_scenes") == []


def test_original_source_tamper_is_detected_on_readback(tmp_path):
    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path / "registry"))
    record = import_radiance_scene(service, _metadata(), _npy(_array()))
    path = service.registry.blob_path(record["source_sha256"])
    path.write_bytes(b"tampered")
    with pytest.raises(IntegrityError, match="checksum"):
        get_radiance_scene(service, record["id"])


def test_v6_job_spec_uses_asset_identity_not_a_hidden_luminance_target():
    spec = default_radiance_scene_spec()
    assert spec["schema"] == RADIANCE_SCENE_SCHEMA
    assert "luminance_cd_m2" not in spec
    assert "scene_kind" not in spec
    spec.update(
        lens_revision_id="lens-revision",
        sensor_revision={"identifier": "sensor", "run_id": "run"},
        radiance_scene_id="radiance-scene-id",
        accept_approximate_lens_radiometry=True,
    )
    assert specification(spec) == spec
    with pytest.raises(ValueError, match="exactly"):
        specification({**spec, "luminance_cd_m2": 1000})


@pytest.mark.parametrize("horizontal_fov_deg", [30.0, 20.0])
def test_v6_rejects_source_fov_beyond_saved_lens_field_before_queue(
    tmp_path, horizontal_fov_deg,
):
    from camera_platform.lens_sensor_raw_job import prepare
    from camera_platform.sensor_db_bridge import sensor_revision_catalog
    from camera_platform.service import PlatformService

    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    service = PlatformService(tmp_path / "state", WORKSPACE)
    metadata = _metadata()
    metadata["horizontal_fov_deg"] = horizontal_fov_deg
    record = import_radiance_scene(service, metadata, _npy(_array()))
    sensor = next(
        row for row in sensor_revision_catalog(root=WORKSPACE / "camerae2e_db")
        if row["report_code"] == "DEF-2512-801"
    )
    spec = default_radiance_scene_spec()
    spec.update(
        lens_revision_id="444be61b-3bbb-51f9-938a-c4823e702d13",
        sensor_revision={"identifier": sensor["report_code"], "run_id": sensor["run_id"]},
        radiance_scene_id=record["id"],
        sensor_size=[8, 8],
        accept_approximate_lens_radiometry=True,
    )
    with pytest.raises(ValueError, match="FOV exceeds the saved Lens DB PSF field"):
        prepare(service, spec)
    assert service.registry.list_runs() == []


def test_v6_template_scope_is_exposed_before_submission(tmp_path):
    from fastapi.testclient import TestClient

    from camera_platform.api import create_app

    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        response = transport.get(
            "/api/v1/lens-sensor-raw/template",
            params={"scope": "central_bounded_radiance_scene"},
        )
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["specification"]["schema"] == RADIANCE_SCENE_SCHEMA
    assert document["resource_contract"]["registered_radiance_unit"] == "W/m^2/sr/nm"
    assert "object-side spectral radiance" in document["limitation"]
    assert "D65 scene" not in document["limitation"]


def test_v6_durable_worker_raw_can_be_reused_by_isp_and_dataset(tmp_path):
    """Use one real DB pair; a user scene does not become a quality reference."""
    from camera_platform import isp_v2
    from camera_platform.api import create_app
    from camera_platform.lens_sensor_raw_job import get, submit
    from camera_platform.retained_raw_dataset import (
        export_retained_raw_array,
        load_retained_lens_sensor_raw,
    )
    from camera_platform.retained_raw_isp_bridge import (
        inspect_lens_sensor_isp_input,
        replay_lens_sensor_raw,
    )
    from camera_platform.sensor_db_bridge import sensor_revision_catalog
    from camera_platform.worker import execute

    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    sensor = next(
        row for row in sensor_revision_catalog(root=WORKSPACE / "camerae2e_db")
        if row["report_code"] == "DEF-2512-801"
    )
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    service = app.state.service
    record = import_radiance_scene(service, _metadata(), _npy(_array()))
    spec = default_radiance_scene_spec()
    spec.update(
        lens_revision_id="4daeca8f-5d81-57b0-8ae3-928f23de2b36",
        sensor_revision={"identifier": sensor["report_code"], "run_id": sensor["run_id"]},
        radiance_scene_id=record["id"], sensor_size=[8, 8],
        accept_approximate_lens_radiometry=True,
    )
    queued = submit(service, spec)
    assert queued["source_identities"]["radiance_scene"] == source_identity(record)
    assert queued["resource_preflight"]["status"] == "within_development_budget"
    assert service.registry.claim()["id"] == queued["id"]
    execute(service, queued["id"])
    finished = get(service, queued["id"])
    assert finished["status"] == "succeeded", finished.get("error")
    summary = finished["result"]["summary"]
    assert summary["preview"]["scene"]["source_asset"] == source_identity(record)
    assert summary["preview"]["scene"]["luminance_cd_m2"] is None
    assert summary["preview"]["raw"]["shape"] == [8, 8]
    assert summary["preview"]["lens_model"]["diffraction_included"] is False
    artifacts = {item["name"]: item for item in finished["artifacts"]}
    report_path = service.registry.blob_path(
        artifacts["lens-sensor-raw-preview-result.json"]["sha256"]
    )
    raw_path = service.registry.blob_path(
        artifacts["lens-sensor-original-raw.npy"]["sha256"]
    )
    report, raw, binding = load_retained_lens_sensor_raw(
        report_path, raw_path, source_lens_sensor_job_id=queued["id"]
    )
    assert report == summary
    assert raw.shape == (8, 8) and raw.dtype == np.int32
    assert binding["source_scene"] == "object_radiance_sha256:" + record["source_sha256"]
    inspected = inspect_lens_sensor_isp_input(
        report_path, raw_path, source_lens_sensor_job_id=queued["id"],
        sensor_db_root=WORKSPACE / "camerae2e_db", profile=isp_v2.default_profile(),
    )
    assert inspected["source_binding"] == binding
    profile = isp_v2.default_profile()
    profile["white_balance"]["gains_rgb"] = [1.2, 1.0, 0.8]
    replayed = replay_lens_sensor_raw(
        report_path, raw_path, source_lens_sensor_job_id=queued["id"],
        sensor_db_root=WORKSPACE / "camerae2e_db", profile=profile,
        output_dir=tmp_path / "replay",
    )
    assert replayed["raw_values_byte_equal"] is True
    assert replayed["before_after"]["default_vs_candidate"]["available"] is True
    assert replayed["source_binding"] == binding
    exported = export_retained_raw_array(
        raw, source_binding=binding, seed=spec["seed"], scene=binding["source_scene"],
        split="test", output_dir=tmp_path / "dataset",
    )
    assert exported["source_binding"] == binding
