"""Independent joint-angle arithmetic and real v3 Factory/API/Worker evidence.

Sensor EQE is explicitly authored synthetic test data. These checks establish
calculation/provenance behavior, never physical sensor accuracy or approval.
"""

import hashlib
import io
import json
import math
import os
import time
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import prescription_psf, sequences, spatial
from camera_platform.api import create_app
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.evidence import build_bundle
from camera_platform.registry import IntegrityError, canonical, digest
from camera_platform.service import PlatformService

WAVE = [500, 600]
SITES = ["R", "Gr", "Gb", "B"]
QE = np.array([0.2, 0.35, 0.6, 0.8])


def _response(pitch, *, anisotropic=False):
    values = np.broadcast_to(QE, (2, 2, 4, 4)).copy()
    if anisotropic:
        for w in range(2):
            for p, phi in enumerate((0, 90, 180, 270)):
                values[w, 1, p] += (
                    np.array([0.05, -0.06, 0.08, -0.1])
                    * (w + 1)
                    * math.sin(math.radians(20))
                    * math.cos(math.radians(phi))
                )
    return {
        "schema": "camera_native_pixel_response_v1",
        "site_names": SITES.copy(),
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": pitch,
        "reference_plane": "sensor_surface_before_pixel_optics",
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "polarization": "unpolarized",
        "response_basis": "independent_site_collected_electrons_per_incident_pixel_photon",
        "wavelength_nm": WAVE.copy(),
        "theta_deg": [0, 20],
        "phi_deg": [0, 90, 180, 270],
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"],
        "values": values.tolist(),
        "source": {"kind": "estimated", "description": "Authored independent acceptance EQE"},
    }


def _pure_request():
    request = spatial.request_template()
    request.pop("qe")
    request.pop("sensor_angular_response")
    request.update(
        wavelength_nm=WAVE.copy(),
        native_pixel_response=_response(4, anisotropic=True),
        noise_enabled=False,
        frames=1,
    )
    request["readout"].update(read_noise_e=0, dark_current_e_per_s=0)
    request["scene"].update(wavelength_nm=WAVE.copy(), spectral_irradiance=np.full((8, 8, 2), 1e-4))
    # Odd full geometry puts actual native center and four cardinal positions
    # inside this even Bayer crop; the CFA remains anchored at native (0,0).
    request["scene"]["sampling"].update(full_sensor_shape=[9, 9], crop_origin=[0, 0])
    request["psf"].update(wavelength_nm=WAVE.copy(), values=[[[1]], [[1]]])
    directions = [
        [[0, 0, 1], [0, 0, 1]],
        [
            [math.sin(math.radians(10)), 0, math.cos(math.radians(10))],
            [0, math.sin(math.radians(20)), math.cos(math.radians(20))],
        ],
    ]
    request["lens"] = {
        "wavelength_nm": WAVE.copy(),
        "field_radius": [0, 1],
        "theta_deg": [0, 10, 20],
        "angular_weights": [[[1, 0, 0], [0, 0.25, 0.75]], [[1, 0, 0], [0, 0.8, 0.2]]],
        "relative_illumination": [[1, 0.7], [1, 0.9]],
        "field_reference_radius_mm": 0.03,
        "directional_response": {
            "schema": "camera_lens_directional_response_v1",
            "wavelength_nm": WAVE.copy(),
            "field_radius": [0, 1],
            "image_height_mm": [0, 0.03],
            "field_reference_radius_mm": 0.03,
            "direction_convention": "propagation_into_sensor_x_column_y_row",
            "reference_plane": "sensor_surface_before_pixel_optics",
            "field_reference": "positive_x_column",
            "symmetry": "centered_spherical_or_plane_prescription_radial_stop",
            "normalization": "conditional_transmitted_irradiance_fraction",
            # Unit-test fixtures are not submitted as Factory models.
            "prescription_sha256": "a" * 64,
            "stop_transmission_sha256": "b" * 64,
            "directions_xyz": directions,
            "conditional_weights": [[[1, 0], [0.25, 0.75]], [[1, 0], [0.8, 0.2]]],
            "on_axis_rotation_phi_deg": list(range(0, 360, 45)),
        },
    }
    return request


def _scalar_eqe(values, band, theta, phi, site):
    # Independent scalar periodic piecewise-linear interpolation at exact WAVE
    # nodes. No native response or lens iteration/interpolation helper is used.
    position = (phi % 360) / 90
    lower = int(math.floor(position))
    fraction = position - lower
    angular = []
    for t in range(2):
        angular.append(
            (1 - fraction) * values[band][t][lower][site]
            + fraction * values[band][t][(lower + 1) % 4][site]
        )
    return angular[0] * (1 - theta / 20) + angular[1] * theta / 20


def test_pure_native_spatial_matches_scalar_joint_phi_mixture_and_actual_raw():
    request = _pure_request()
    output = spatial.SpatialSceneRuntime().execute(request)
    expected = np.zeros((8, 8, 4))
    values = request["native_pixel_response"]["values"]
    for row in range(8):
        for col in range(8):
            x, y = (col - 4) * 0.004, (row - 4) * 0.004
            radius = math.hypot(x, y) / 0.03
            field_phi = math.degrees(math.atan2(y, x))
            for site in range(4):
                densities = []
                for band, wavelength in enumerate(WAVE):
                    weights = [0.25, 0.75] if band == 0 else [0.8, 0.2]
                    edge = sum(
                        weight * _scalar_eqe(values, band, theta, phi + field_phi, site)
                        for weight, theta, phi in zip(weights, (10, 20), (0, 90), strict=True)
                    )
                    center = values[band][0][0][site]
                    eqe = (1 - radius) * center + radius * edge
                    ri = (1 - radius) + radius * ([0.7, 0.9][band])
                    photons = 1e-4 * ri * wavelength * 1e-9 / (6.62607015e-34 * 299792458)
                    densities.append(photons * eqe)
                expected[row, col, site] = (
                    sum(densities) * 100 / 2 * (4e-6) ** 2 * request["exposure_s"]
                )
    arrays = output["arrays"]
    np.testing.assert_allclose(arrays["channel_electrons"], expected, rtol=3e-13, atol=1e-12)
    cfa = np.array([[0, 1], [2, 3]])[np.arange(8)[:, None] % 2, np.arange(8)[None, :] % 2]
    charge = np.take_along_axis(expected, cfa[..., None], axis=-1)[..., 0]
    scale = (4095 - 64) / request["readout"]["full_well_e"]
    np.testing.assert_array_equal(arrays["raw_frames"][0], np.rint(64 + charge * scale))
    # Same native site at east/north/west/south. The azimuth-sensitive response
    # changes real RAW while the physical radius, RI, site and scene stay equal.
    cardinal = [(4, 6), (6, 4), (4, 2), (2, 4)]
    actual_dn = [int(arrays["raw_frames"][0, row, col]) for row, col in cardinal]
    assert len(set(actual_dn)) > 1
    # A deliberately wrong independent theta/phi reconstruction changes the
    # product expectation: Q has theta and phi correlation at this node.
    joint = 0.25 * _scalar_eqe(values, 0, 10, 0, 0) + 0.75 * _scalar_eqe(values, 0, 20, 90, 0)
    factorized = sum(
        tw * pw * _scalar_eqe(values, 0, theta, phi, 0)
        for tw, theta in zip((0.25, 0.75), (10, 20), strict=True)
        for pw, phi in zip((0.25, 0.75), (0, 90), strict=True)
    )
    assert abs(joint - factorized) > 0.001


def _fields(record, contract):
    return {key: deepcopy(value) for key, value in record.items() if key in contract.model_fields}


def _post(client, path, payload, status=201):
    response = client.post("/api/v1/" + path, json=payload)
    assert response.status_code == status, response.text
    return response.json()


def _get(client, path, **params):
    response = client.get("/api/v1/" + path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _wait(client, queued, *, factory=False):
    path = ("factory/jobs/" if factory else "runs/") + queued["id"]
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        record = _get(client, path)
        if record["status"] in ("succeeded", "failed", "cancelled"):
            assert record["status"] == "succeeded", record.get("error") or record
            return record
        time.sleep(0.1)
    pytest.fail("Actual Worker did not finish the bounded native test in 90 seconds")


def _array(service, record, name):
    artifact = next(a for a in record["artifacts"] if a["name"] == name + ".npy")
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    return np.load(io.BytesIO(content), allow_pickle=False)


@pytest.fixture(scope="module")
def workflow(tmp_path_factory):
    pytest.importorskip("rayoptics")
    pytest.importorskip("prysm")
    directory = tmp_path_factory.mktemp("native-pixel-api-worker")
    app = create_app(directory, start_worker=True)
    with TestClient(app) as client:
        client.headers["X-Platform-Token"] = _get(client, "session")["token"]
        service = app.state.service
        assert _get(client, "health")["worker"] == "running"
        assert app.state.worker.pid != os.getpid()
        original = service.registry.list("assemblies")[0]
        # Keep every actual v3 factory template input and convergence gate.
        prescription = prescription_psf.request_template(version=3)
        psf_input = _get(
            client, "templates/factory", plugin_id="prescription_psf_v1", request_version=3
        )
        assert psf_input["request"] == prescription
        psf_job = _wait(client, _post(client, "factory/jobs", psf_input, 202), factory=True)
        assert psf_job["result"]["summary"]["status"] == "PROPOSED"
        ray_input = _get(
            client, "templates/factory", plugin_id="rayoptics_lens_v1", request_version=3
        )
        ray_input["request"] = prescription["prescription"]
        ray_job = _wait(client, _post(client, "factory/jobs", ray_input, 202), factory=True)
        assert ray_job["result"]["summary"]["status"] == "PROPOSED"
        conditions = deepcopy(original["conditions"])
        conditions["f_number"] = {
            "value": psf_job["result"]["summary"]["geometric_reference_f_number"],
            "unit": "1",
        }
        parent = _post(
            client,
            "factory/models",
            {
                "job_id": ray_job["id"],
                "name": "Actual v3 joint directions",
                "revision": "1",
                # The linear optical parent is deliberately declared before
                # publication without downstream exposure/gain restrictions.
                # PSF composition must preserve every actual parent bound.
                "conditions": {
                    k: v for k, v in conditions.items() if k not in {"exposure_s", "gain"}
                },
            },
        )
        publication = {
            "job_id": psf_job["id"],
            "base_lens_model_id": parent["id"],
            "name": "Actual v3 matched PSF",
            "revision": "1",
            "conditions": conditions,
            "condition_scope": "optical_response",
        }
        lens = _post(client, "factory/models", publication)
        source_sensor = next(
            service.registry.get("models", identity)
            for identity in original["model_ids"]
            if service.registry.get("models", identity)["kind"] == "sensor"
        )
        sensor_data = _fields(source_sensor, ModelCreate)
        sensor_data.update(
            name="Authored unequal four-site sensor",
            revision="native-unit-1",
            parents=[source_sensor["id"]],
            source_kind="estimated",
            source_uri="fixture://independent-native-eqe",
            method_and_version="authored-native-acceptance:1",
            parameter_uncertainty={},
            uncertainty={"kind": "unknown"},
        )
        sensor_data["spectral_range"] = {"min": 500, "max": 600, "unit": "nm"}
        for key in ("active_height", "active_width"):
            sensor_data["properties"][key] = {"value": 0.024, "unit": "mm"}
        sensor_data["properties"]["accepted_angle_range"] = {"min": 0, "max": 20, "unit": "deg"}
        sensor_data["payload"] = {
            "wavelength_nm": WAVE.copy(),
            "pixel_pitch_um": 3.0,
            "readout": deepcopy(source_sensor["payload"]["readout"]),
            "native_pixel_response": _response(3.0),
        }
        sensor = _post(client, "models", sensor_data)
        camera = _fields(original, AssemblyCreate)
        camera.update(
            name="V3 native 8x8 acceptance",
            conditions=conditions,
            spectral_range=sensor_data["spectral_range"],
            model_ids=[lens["id"], sensor["id"], original["model_ids"][2]],
        )
        assembly = _post(client, "assemblies", camera)
        assert assembly["compatibility"]["status"] == "PASS", assembly["compatibility"]
        metadata = _get(client, "templates/spectral-image", assembly_id=assembly["id"])
        metadata.update(
            name="Known uniform two-band native irradiance",
            source_uri="fixture://native-uniform-optical-image",
            method_and_version="authored-sensor-surface-irradiance:1",
        )
        content = io.BytesIO()
        np.save(content, np.full((8, 8, 2), 1e-3), allow_pickle=False)
        imported = client.post(
            "/api/v1/spectral_images/import",
            data={"metadata": json.dumps(metadata)},
            files={"image": ("image.npy", content.getvalue(), "application/octet-stream")},
        )
        assert imported.status_code == 201, imported.text
        image = imported.json()
        spatial_test = _post(
            client,
            "tests",
            _get(
                client, "templates/spatial-test", assembly_id=assembly["id"], scene_id=image["id"]
            ),
        )
        spatial_request = {
            "assembly_id": assembly["id"],
            "test_id": spatial_test["id"],
            "scene_id": image["id"],
            "plugin_id": "spatial_scene_v1",
            "parameters": {"frames": 2, "seed": 74},
        }
        spatial_run = _wait(client, _post(client, "runs", spatial_request, 202))
        scene_data = sequences.scene_template(service, assembly["id"])
        for knot in scene_data["payload"]["knots"]:
            knot["spectral_image_id"] = image["id"]
        scene = _post(client, "scene_sequences", scene_data)
        controls = _get(
            client,
            "templates/control-sequence",
            assembly_id=assembly["id"],
            scene_sequence_id=scene["id"],
        )
        controls["payload"]["frames"] = controls["payload"]["frames"][:2]
        for index, frame in enumerate(controls["payload"]["frames"]):
            frame["applied"]["exposure_ns"] = (index + 1) * 2_000_000
        control = _post(client, "control_sequences", controls)
        program = _post(
            client,
            "capture_programs",
            _get(client, "templates/capture-program", assembly_id=assembly["id"]),
        )
        temporal_test = _post(
            client,
            "tests",
            _get(
                client,
                "templates/temporal-test",
                assembly_id=assembly["id"],
                scene_sequence_id=scene["id"],
                control_sequence_id=control["id"],
                capture_program_id=program["id"],
            ),
        )
        hdr_request = {
            "assembly_id": assembly["id"],
            "test_id": temporal_test["id"],
            "scene_sequence_id": scene["id"],
            "control_sequence_id": control["id"],
            "capture_program_id": program["id"],
            "plugin_id": "hdr_bracket_v1",
            "parameters": {"seed": 748},
        }
        hdr_run = _wait(client, _post(client, "runs", hdr_request, 202))
        yield {
            "client": client,
            "service": service,
            "directory": directory,
            "lens": lens,
            "parent": parent,
            "sensor": sensor,
            "sensor_data": sensor_data,
            "assembly": assembly,
            "ray_job": ray_job,
            "psf_job": psf_job,
            "publication": publication,
            "spatial_run": spatial_run,
            "hdr_run": hdr_run,
            "spatial_request": spatial_request,
            "hdr_request": hdr_request,
        }


def test_actual_v3_factory_original_responses_reach_native_spatial_and_hdr_raw(workflow):
    data, service = workflow, workflow["service"]
    response = data["lens"]["payload"]["directional_response"]
    assert data["lens"]["parents"] == [data["parent"]["id"]]
    assert response["prescription_sha256"] == digest(data["ray_job"]["request"])
    assert data["psf_job"]["request"]["prescription"] == data["ray_job"]["request"]
    for model, field in (
        (data["lens"], "directional_response"),
        (data["sensor"], "native_pixel_response"),
    ):
        descriptor = model["response_artifacts"][field]
        content = service.registry.blob_path(descriptor["sha256"]).read_bytes()
        assert content == canonical(model["payload"][field]).encode()
        assert hashlib.sha256(content).hexdigest() == descriptor["sha256"]
    for kind in ("spatial", "hdr"):
        run = data[kind + "_run"]
        summary = run["result"]["summary"]
        assert summary["native_pixel_response"]["site_names"] == SITES
        assert summary["native_pixel_response"]["directional_response_sha256"] == digest(response)
        assert summary["physical_validation"] == "not_established"
        cfa = _array(service, run, "native_site_index")
        raw = _array(service, run, "raw_frames")
        assert raw.shape == (2, 8, 8) and raw.dtype.kind in "iu"
        assert raw[:, cfa == 1].mean() < raw[:, cfa == 2].mean()
        channels = _array(service, run, "channel_electrons")
        np.testing.assert_allclose(channels[..., 2], channels[..., 1] * (0.6 / 0.35), rtol=1e-12)
        np.testing.assert_array_equal(
            _array(service, run, "native_eqe"),
            data["sensor"]["payload"]["native_pixel_response"]["values"],
        )
        np.testing.assert_array_equal(
            _array(service, run, "lens_direction_xyz"), response["directions_xyz"]
        )
        np.testing.assert_array_equal(
            _array(service, run, "lens_direction_power_fraction"), response["conditional_weights"]
        )
    sensor_image = _array(service, data["spatial_run"], "sensor_spectral_irradiance")
    photons = sensor_image * np.array(WAVE) * 1e-9 / (6.62607015e-34 * 299792458)
    fluence = photons.sum(axis=-1) * 100 / 2 * (3e-6) ** 2
    expected = fluence[..., None] * QE * data["spatial_run"]["request"]["exposure_s"]
    np.testing.assert_allclose(
        _array(service, data["spatial_run"], "channel_electrons"), expected, rtol=1e-12
    )
    temporal = _array(service, data["hdr_run"], "mean_electrons")
    np.testing.assert_allclose(temporal[1], temporal[0] * 2, rtol=1e-13)
    assert data["hdr_run"]["result"]["summary"]["hdr_merge_applied"] is True
    assert _array(service, data["hdr_run"], "hdr_valid_mask").any()


@pytest.mark.parametrize("kind", ["spatial", "hdr"])
def test_worker_rejects_frozen_green_response_substitution(workflow, kind):
    forged = deepcopy(workflow[kind + "_run"])
    values = forged["request"]["native_pixel_response"]["values"]
    for wave in values:
        for theta in wave:
            for row in theta:
                row[1], row[2] = row[2], row[1]
    # HDR reconstruction can reject the forged request before native-site
    # verification, at the earlier frozen electrical source-evidence gate.
    with pytest.raises(ValueError, match="[Nn]ative|[Ss]ensor|frozen|fixed source evidence"):
        workflow["service"].execute_run(forged)


@pytest.mark.parametrize("which", ["sensor_response", "factory_phi"])
def test_corrupt_response_or_factory_phi_blocks_cached_submission_and_execution(workflow, which):
    service = workflow["service"]
    if which == "sensor_response":
        artifact = workflow["sensor"]["response_artifacts"]["native_pixel_response"]
    else:
        artifact = next(
            a
            for a in workflow["ray_job"]["artifacts"]
            if a["name"] == "on_axis_rotation_phi_deg.npy"
        )
    path = service.registry.blob_path(artifact["sha256"])
    saved = path.read_bytes()
    before = len(service.registry.list_runs())
    try:
        path.write_bytes(b"deliberately corrupted isolated test response")
        response = workflow["client"].post("/api/v1/runs", json=workflow["spatial_request"])
        assert response.status_code in (409, 422), response.text
        assert len(service.registry.list_runs()) == before
        with pytest.raises((IntegrityError, ValueError)):
            service.execute_run(deepcopy(workflow["hdr_run"]))
        if which == "factory_phi":
            refused = workflow["client"].post(
                "/api/v1/factory/models", json=workflow["publication"]
            )
            assert refused.status_code in (409, 422), refused.text
    finally:
        path.write_bytes(saved)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    cached = _post(workflow["client"], "runs", workflow["spatial_request"], 202)
    assert cached["id"] == workflow["spatial_run"]["id"]


@pytest.mark.parametrize(
    "mutation", ["duplicate_optics", "cell_normalization", "lens_direction_edit"]
)
def test_api_rejects_double_counted_or_unbound_response_models(workflow, mutation):
    payload = deepcopy(workflow["sensor_data"])
    if mutation == "duplicate_optics":
        payload["payload"]["qe"] = [[0.2, 0.2], [0.4, 0.4], [0.8, 0.8]]
    elif mutation == "cell_normalization":
        payload["payload"]["native_pixel_response"]["response_basis"] = (
            "periodic_cell_absorption_fraction"
        )
    else:
        payload = _fields(workflow["lens"], ModelCreate)
        payload["payload"]["directional_response"]["conditional_weights"][0][0].reverse()
    before = len(workflow["service"].registry.list("models"))
    refused = workflow["client"].post("/api/v1/models", json=payload)
    if mutation == "lens_direction_edit":
        # The original uncompressed v3 lens exceeds the generic 2 MiB body cap;
        # its HTTP size refusal is not evidence that provenance was inspected.
        assert refused.status_code == 413, refused.text
        with pytest.raises(ValueError, match="preserved PSF|Composite Lens|Factory"):
            workflow["service"].create_model(payload)
    else:
        assert refused.status_code == 422, refused.text
    assert len(workflow["service"].registry.list("models")) == before


def test_reopened_records_and_bundle_keep_all_original_response_and_raw_bytes(workflow):
    service = workflow["service"]
    reopened = PlatformService(workflow["directory"])
    before = service.registry.audit()
    for kind in ("spatial", "hdr"):
        record = workflow[kind + "_run"]
        assert reopened.registry.get_run(record["id"]) == record
        with zipfile.ZipFile(build_bundle(reopened, "runs", record["id"])) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            assert manifest["run"] == record
            for artifact in record["artifacts"]:
                content = bundle.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
                assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
                assert reopened.registry.blob_path(artifact["sha256"]).read_bytes() == content
        http_record = _get(workflow["client"], "runs/" + record["id"])
        assert http_record == record
    for model in (workflow["sensor"], workflow["lens"]):
        assert reopened.registry.get("models", model["id"]) == model
    after = reopened.registry.audit()
    assert before["events"] == after["events"]
    assert before["head_hash"] == after["head_hash"]
    assert after["integrity"] == "verified"
