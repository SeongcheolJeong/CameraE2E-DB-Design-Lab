#!/usr/bin/env python3
"""Verify native four-site optics/RAW/HDR through an isolated live API and Worker.

Every POST intent and returned identity is journaled. Rerunning resumes saved
identities and verifies their bytes; an uncertain POST requires reconciliation.
The independent 8x8 reference uses direct source-pixel PSF sums and the authored
analytic EQE LUT, not simulator electron arrays or runtime integration helpers.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import numpy as np

from camera_platform import __version__
from camera_platform.contracts import AssemblyCreate, ModelCreate, SceneSequenceCreate
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient

SITE_NAMES = ["R", "Gr", "Gb", "B"]
SITE_BASE = np.array([0.2, 0.35, 0.6, 0.8])
SITE_SLOPE = np.array([0.01, 0.015, -0.01, -0.015])


def fields(record, contract):
    return {key: copy.deepcopy(value) for key, value in record.items()
            if key in contract.model_fields}


def native_response(pitch):
    """An explicit numerical fixture, not inferred anisotropy or a measured sensor."""
    theta, phi = [0, 20], [0, 90, 180, 270]
    values = [[[(SITE_BASE + SITE_SLOPE * angle / 20 * np.cos(np.deg2rad(azimuth))).tolist()
                for azimuth in phi] for angle in theta] for _ in (500, 600)]
    return {
        "schema": "camera_native_pixel_response_v1", "site_names": SITE_NAMES.copy(),
        "site_index_tile": [[0, 1], [2, 3]], "pixel_pitch_um": pitch,
        "reference_plane": "sensor_surface_before_pixel_optics",
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "polarization": "unpolarized",
        "response_basis": "independent_site_collected_electrons_per_incident_pixel_photon",
        "wavelength_nm": [500, 600], "theta_deg": theta, "phi_deg": phi,
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"], "values": values,
        "source": {"kind": "estimated", "description": (
            "Authored acceptance fixture: unequal Gr/Gb and small azimuth asymmetry. "
            "Independent sites, no measured pixel response or physical validation.")},
    }


def sensor_candidate(base, pitch):
    """Build the complete Sensor metadata alongside the authored response."""
    candidate = fields(base, ModelCreate)
    candidate.update(name="Native QA · 합성 R/Gr/Gb/B/방위각 응답", revision="native-qa-1",
        source_kind="estimated", source_uri="fixture://native-four-site-acceptance",
        method_and_version="authored-independent-site-absolute-eqe:1", parents=[base["id"]],
        physical_instance=None, uncertainty={"kind": "unknown"}, parameter_uncertainty={})
    candidate["spectral_range"] = {"min": 500, "max": 600, "unit": "nm"}
    candidate["payload"] = {"wavelength_nm": [500, 600], "pixel_pitch_um": pitch,
        "native_pixel_response": native_response(pitch),
        "readout": {**base["payload"]["readout"], "read_noise_e": 0, "dark_current_e_per_s": 0}}
    candidate["properties"]["accepted_angle_range"] = {"min": 0, "max": 20, "unit": "deg"}
    for key in ("active_width", "active_height"):
        candidate["properties"][key] = {"value": pitch * 8 / 1000, "unit": "mm"}
    return candidate


def independent_rate(image, sampling, lens):
    """Small direct sum, independent of platform convolution and EQE operators.

    The exact supplied PSF samples and joint ray power are inputs, not independent
    lens truth. This checks their numerical application, native origin and units.
    """
    rows, cols, bands = image.shape
    if (rows, cols, bands) != (8, 8, 2):
        raise ValueError("This independent acceptance reference is explicitly 8x8x2")
    pitch = sampling["pixel_pitch_um"]
    origin, full = sampling["crop_origin"], sampling["full_sensor_shape"]
    y = (np.arange(rows) + origin[0] - (full[0] - 1) / 2) * pitch / 1000
    x = (np.arange(cols) + origin[1] - (full[1] - 1) / 2) * pitch / 1000
    radius = np.hypot(y[:, None], x[None, :])
    azimuth = np.degrees(np.arctan2(y[:, None], x[None, :]))
    psf = lens["psf"]
    kernels = np.asarray(psf["values"], dtype=float)
    if psf["schema"] != "field_intensity_psf_v1":
        raise ValueError("Acceptance requires the actual v3 field PSF Factory result")

    def brackets(axis, position):
        axis = np.asarray(axis, dtype=float)
        if position < axis[0] or position > axis[-1]:
            raise ValueError("Independent reference refuses field extrapolation")
        upper = int(np.clip(np.searchsorted(axis, position, side="right"), 1, len(axis) - 1))
        fraction = (position - axis[upper - 1]) / (axis[upper] - axis[upper - 1])
        return ((upper - 1, 1 - fraction), (upper, fraction))

    output = np.zeros_like(image)
    ry, rx = np.indices((rows, cols))
    for sy in range(rows):
        for sx in range(cols):
            ky, kx = ry - sy + kernels.shape[-2] // 2, rx - sx + kernels.shape[-1] // 2
            valid = (ky >= 0) & (ky < kernels.shape[-2]) & (kx >= 0) & (kx < kernels.shape[-1])
            for fy, wy in brackets(psf["field_y_mm"], y[sy]):
                for fx, wx in brackets(psf["field_x_mm"], x[sx]):
                    for band in range(bands):
                        output[..., band][valid] += (image[sy, sx, band] * wy * wx
                            * kernels[band, fy, fx, ky[valid], kx[valid]])
    directional = lens["directional_response"]
    vectors = np.asarray(directional["directions_xyz"], dtype=float)
    powers = np.asarray(directional["conditional_weights"], dtype=float)
    rates = np.zeros((rows, cols, 4))
    spectral_terms = []
    for band, wavelength in enumerate((500, 600)):
        averaged = np.zeros((rows, cols, 4))
        for iy in range(rows):
            for ix in range(cols):
                r = radius[iy, ix] / directional["field_reference_radius_mm"]
                for field, field_weight in brackets(directional["field_radius"], r):
                    mask = powers[band, field] > 0
                    direction, power = vectors[field, mask], powers[band, field, mask]
                    polar = np.degrees(np.arccos(np.clip(direction[:, 2], -1, 1)))
                    base_phi = np.degrees(np.arctan2(direction[:, 1], direction[:, 0]))
                    rotations = directional["on_axis_rotation_phi_deg"] if field == 0 else [0]
                    response = np.zeros(4)
                    for rotation in rotations:
                        phi = (base_phi + rotation + azimuth[iy, ix]) % 360
                        # The authored LUT interpolates these cosine samples linearly.
                        triangle = np.interp(phi, [0, 90, 180, 270, 360], [1, 0, -1, 0, 1])
                        q = SITE_BASE + (polar / 20 * triangle)[:, None] * SITE_SLOPE
                        response += (q * power[:, None]).sum(axis=0) / len(rotations)
                    averaged[iy, ix] += field_weight * response
        ri = np.interp(radius / lens["field_reference_radius_mm"], lens["field_radius"],
                       lens["relative_illumination"][band])
        photons = output[..., band] * wavelength * 1e-9 / (6.62607015e-34 * 299792458)
        spectral_terms.append(photons[..., None] * ri[..., None] * averaged)
    rates[:] = (pitch * 1e-6) ** 2 * (600 - 500) * (spectral_terms[0] + spectral_terms[1]) / 2
    sites = np.array([[0, 1], [2, 3]])[(np.arange(rows)[:, None] + origin[0]) % 2,
                                      (np.arange(cols)[None, :] + origin[1]) % 2]
    return rates, sites


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-state-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if (address.scheme != "http" or address.hostname != "127.0.0.1"
            or address.port in (None, 8770) or address.username or address.password
            or address.path not in ("", "/") or address.query or address.fragment):
        parser.error("Require an isolated explicit loopback HTTP port; primary 8770 is refused")
    state, out = args.expected_state_dir.resolve(), args.output_dir.resolve()
    if state.name == "camera-platform":
        parser.error("Primary state is refused")
    out.mkdir(parents=True, exist_ok=True)
    journal = out / "summary.json"
    report = json.loads(journal.read_text()) if journal.exists() else {
        "schema": "camera_native_pixel_http_verification_v1", "status": "in_progress",
        "base_url": args.base_url, "state_dir": str(state), "version": __version__,
        "checks": [], "operations": {}, "artifacts": {}, "packages": {},
        "source_kind": "simulated", "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
        "scope": ["actual immutable API and supervised Worker execution",
                  "direct 8x8 PSF/joint-direction/EQE application reference",
                  "unequal native greens, exposure integration, sampled RAW and RAW-only HDR",
                  "source/result byte hashes and re-reading the same record identities"],
        "not_proven": ["physical sensor or lens accuracy", "measured azimuth anisotropy",
                       "general inter-site charge transfer", "motion HDR or DCG",
                       "independent packaged installation", "restart unless rerun after restart"],
    }
    if (report["base_url"] != args.base_url or report["state_dir"] != str(state)
            or report["version"] != __version__):
        raise ValueError("Resume requires the identical endpoint, state and source milestone")
    report["status"] = "in_progress"

    def save():
        report["updated_at"] = datetime.now(UTC).isoformat()
        temporary = journal.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(journal)

    def check(label, condition):
        if not condition:
            report["status"] = "failed"
            report["last_failed_check"] = label
            save()
            raise AssertionError(label)
        if label not in report["checks"]:
            report["checks"].append(label)
        save()

    with PlatformClient(args.base_url, timeout=60) as client:
        health, session = client.get("health"), client.get("session")
        check("isolated supervised Worker identity", Path(health["state_dir"]).resolve() == state
              and health["worker"] == "running" and health["status"] == "ok")
        check("source version and workspace identity", session["version"] == __version__
              and Path(session["state_dir"]).resolve() == state
              and Path(session["workspace"]).resolve() == Path(health["workspace"]).resolve())

        def preserve(label, record):
            (out / (label + "-record.json")).write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            return record

        def send(label, endpoint, payload, collection=None):
            operations = report["operations"]
            if label in operations:
                old = operations[label]
                if old["payload_sha256"] != digest(payload) or old["endpoint"] != endpoint:
                    raise ValueError("Changed saved intent requires reconciliation: " + label)
                if not old.get("id"):
                    raise RuntimeError("Reconcile saved POST before retrying: " + label)
                record = client.get((collection or endpoint) + "/" + old["id"])
                if old.get("record_sha256") and digest(record) != old["record_sha256"]:
                    raise ValueError("Saved immutable record changed: " + label)
                return preserve(label, record)
            operations[label] = {"endpoint": endpoint, "collection": collection or endpoint,
                "payload": payload, "payload_sha256": digest(payload), "status": "intent_saved"}
            save()
            try:
                record = client.post(endpoint, payload)
            except httpx.HTTPStatusError as exc:
                operations[label].update(status="http_error_requires_reconciliation",
                    http_status=exc.response.status_code, response_body=exc.response.text[:20000])
                save()
                raise
            except Exception as exc:
                operations[label].update(status="uncertain_post", error_type=type(exc).__name__)
                save()
                raise
            operations[label].update(id=record["id"], status="response_received")
            if endpoint not in {"runs", "factory/jobs"}:
                operations[label]["record_sha256"] = digest(record)
            save()
            return preserve(label, record)

        def wait(label, job, factory=False):
            result = (client.wait_characterization(job["id"], timeout=300) if factory
                      else client.wait(job["id"], timeout=300))
            preserve(label, result)
            check(label + " succeeded", result["status"] == "succeeded")
            report["operations"][label]["record_sha256"] = digest(result)
            save()
            return result

        def artifact(descriptor):
            name, sha = descriptor["name"], descriptor["sha256"]
            if Path(name).name != name or len(sha) != 64:
                raise ValueError("Refuse malformed artifact path")
            response = client.http.get(descriptor["url"])
            response.raise_for_status()
            content = response.content
            check("artifact SHA256 " + sha, hashlib.sha256(content).hexdigest() == sha
                  and len(content) == descriptor["bytes"])
            target = out / "artifacts" / sha / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            report["artifacts"][sha + "/" + name] = descriptor
            save()
            return content

        def array(record, name):
            matches = [a for a in record["artifacts"] if a["name"] == name + ".npy"]
            if len(matches) != 1:
                raise ValueError("Array must have one unambiguous descriptor: " + name)
            return np.load(io.BytesIO(artifact(matches[0])), allow_pickle=False)

        def package(label, run):
            response = client.http.get("/api/v1/runs/" + run["id"] + "/bundle")
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                check(label + " package run identity", manifest["run"]["id"] == run["id"])
                for item in archive.namelist():
                    if item.startswith("artifacts/"):
                        _, sha, _ = item.split("/", 2)
                        check(label + " package artifact " + sha,
                              hashlib.sha256(archive.read(item)).hexdigest() == sha)
                for model in run["model_snapshots"]:
                    for descriptor in model.get("response_artifacts", {}).values():
                        path = f"artifacts/{descriptor['sha256']}/{descriptor['name']}"
                        check(label + " package response " + descriptor["sha256"],
                              hashlib.sha256(archive.read(path)).hexdigest()
                              == descriptor["sha256"])
            target = out / (label + "-evidence.zip")
            target.write_bytes(response.content)
            report["packages"][label] = {"path": target.name, "bytes": len(response.content),
                                        "sha256": hashlib.sha256(response.content).hexdigest()}
            save()

        if "original_assembly_id" not in report:
            originals = client.get("assemblies")
            if not originals:
                raise ValueError("QA workspace must contain its authored demo assembly")
            report["original_assembly_id"] = originals[0]["id"]
            save()
        original = client.get("assemblies/" + report["original_assembly_id"])
        base_models = {m["kind"]: m for m in
                       (client.get("models/" + identity) for identity in original["model_ids"])}

        psf_draft = client.factory_template("prescription_psf_v1", request_version=3)
        psf_draft["name"] = "Native QA · 실제 v3 처방 PSF"
        psf_job = wait("psf", send("psf", "factory/jobs", psf_draft), factory=True)
        check("v3 PSF numerical gates", psf_job["result"]["summary"]["status"] == "PROPOSED")
        ray_draft = client.factory_template("rayoptics_lens_v1", request_version=3)
        ray_draft.update(name="Native QA · 동일 처방 결합 방향/절대 광량",
                         request=copy.deepcopy(psf_draft["request"]["prescription"]))
        ray_job = wait("ray", send("ray", "factory/jobs", ray_draft), factory=True)
        check("v3 ray numerical gates", ray_job["result"]["summary"]["status"] == "PROPOSED")
        conditions = copy.deepcopy(original["conditions"])
        conditions.update(exposure_s={"value": .002, "unit": "s"},
            f_number={"value": psf_job["result"]["summary"]["geometric_reference_f_number"],
                      "unit": "1"})
        parent = send("lens-parent", "factory/models", {
            "job_id": ray_job["id"], "name": "Native QA · 처방 방향 분포", "revision": "1",
            "conditions": {key: value for key, value in conditions.items()
                           if key not in {"exposure_s", "gain"}}}, collection="models")
        lens = send("lens", "factory/models", {
            "job_id": psf_job["id"], "base_lens_model_id": parent["id"],
            "name": "Native QA · 처방 PSF/방향 결합 렌즈", "revision": "1",
            "conditions": conditions, "condition_scope": "optical_response"}, collection="models")
        check("matching prescription and stop identities", all(
            lens["payload"]["directional_response"][key] == lens["payload"]["radiometry"][key]
            for key in ("prescription_sha256", "stop_transmission_sha256")))
        pitch = psf_draft["request"]["pixel_pitch_um"]
        sensor_draft = sensor_candidate(base_models["sensor"], pitch)
        sensor = send("sensor", "models", sensor_draft)
        camera_draft = fields(original, AssemblyCreate)
        camera_draft.update(name="Native QA · 8x8 4사이트 카메라", revision="native-qa-1",
            model_ids=[lens["id"], sensor["id"], base_models["isp"]["id"]],
            conditions=conditions, spectral_range=sensor["spectral_range"])
        camera = send("camera", "assemblies", camera_draft)
        check("compatible native camera", camera["compatibility"]["status"] == "PASS")
        fixture_draft = client.template("temporal-fixture", assembly_id=camera["id"])
        fixture_draft.update(name="Native QA · 명시적 정적 분광 입력", pattern="static",
            shape=[8, 8], knot_count=2, duration_ns=40_000_000, irradiance_scale=.003)
        fixture = send("fixture", "scene_sequences/fixture", fixture_draft,
                       collection="scene_sequences")
        scene_draft = fields(fixture, SceneSequenceCreate)
        scene_draft.update(name="Native QA · 동일 영상의 정적 시간 축", revision="static-1")
        image_id = scene_draft["payload"]["knots"][0]["spectral_image_id"]
        for knot in scene_draft["payload"]["knots"]:
            knot["spectral_image_id"] = image_id
        scene = send("scene", "scene_sequences", scene_draft)
        image_record = preserve("image", client.get("spectral_images/" + image_id))
        image = array(image_record, "spectral-image")
        expected_rate, sites = independent_rate(image, image_record["sampling"], lens["payload"])
        np.save(out / "independent-site-rates.npy", expected_rate, allow_pickle=False)

        spatial_test = send("spatial-test", "tests", client.template("spatial-test",
            assembly_id=camera["id"], scene_id=image_id))
        spatial_request = {"assembly_id": camera["id"], "test_id": spatial_test["id"],
            "scene_id": image_id, "plugin_id": "spatial_scene_v1",
            "parameters": {"frames": 2, "seed": 613}}
        spatial_run = wait("spatial-run", send("spatial-run", "runs", spatial_request))
        spatial_raw = array(spatial_run, "raw_frames")
        expected_photo = np.take_along_axis(expected_rate, sites[..., None], -1)[..., 0]
        check("direct reference matches spatial photocharge", np.allclose(
            array(spatial_run, "mean_electrons"), expected_photo * .002, rtol=3e-10, atol=1e-10))
        check("spatial actual RAW retains two native frames", spatial_raw.shape == (2, 8, 8)
              and spatial_raw.dtype.kind in "iu")
        check("spatial native site map", np.array_equal(array(spatial_run, "native_site_index"),
                                                        sites))

        controls = client.template("control-sequence", assembly_id=camera["id"],
                                   scene_sequence_id=scene["id"])
        controls["name"] = "Native QA · 명시적 2ms/4ms 독립 노출"
        controls["payload"]["mode"].update(shutter="global", line_time_ns=0)
        controls["payload"]["frames"] = controls["payload"]["frames"][:2]
        if len(controls["payload"]["frames"]) != 2:
            raise ValueError("Authored timeline must cover exactly two selected frame windows")
        for index, frame in enumerate(controls["payload"]["frames"]):
            frame["applied"]["exposure_ns"] = (index + 1) * 2_000_000
        control = send("control", "control_sequences", controls)
        program_draft = client.template("capture-program", assembly_id=camera["id"])
        program_draft["name"] = "Native QA · 전기 RAW와 정적 HDR"
        program = send("program", "capture_programs", program_draft)
        hdr_test = send("hdr-test", "tests", client.template("temporal-test",
            assembly_id=camera["id"], scene_sequence_id=scene["id"],
            control_sequence_id=control["id"], capture_program_id=program["id"]))
        hdr_request = {"assembly_id": camera["id"], "test_id": hdr_test["id"],
            "scene_sequence_id": scene["id"], "control_sequence_id": control["id"],
            "capture_program_id": program["id"], "plugin_id": "hdr_bracket_v1",
            "parameters": {"seed": 927}}
        run = wait("hdr-run", send("hdr-run", "runs", hdr_request))
        summary = run["result"]["summary"]
        raw, charge = array(run, "raw_frames"), array(run, "mean_electrons")
        check("HDR frame/site axes", summary["channels"] == SITE_NAMES
              and summary["prediction"]["axes"][-1]["values"] == SITE_NAMES
              and raw.shape == (2, 8, 8) and raw.dtype.kind in "iu")
        check("independent optical rate matches temporal site rates", np.allclose(
            array(run, "channel_photoelectron_rate_scenes"), expected_rate[None],
            rtol=3e-10, atol=1e-8))
        check("mean charge follows exact 1x/2x exposure", np.array_equal(charge[1], 2 * charge[0])
              and np.allclose(charge[0], expected_photo * .002, rtol=3e-10, atol=1e-10))
        check("charge remains below full well", charge.max() <
              program["payload"]["electrical_model"]["full_well_e"])
        check("both native greens survive actual RAW", all(
            raw[index][sites == 1].mean() < raw[index][sites == 2].mean() for index in (0, 1)))
        model = program["payload"]["electrical_model"]
        white, black = 2**model["adc"]["bit_depth"] - 1, model["adc"]["black_level_dn"]
        scale = model["conversion_gain_v_per_e"] * white / model["adc"]["full_scale_v"]
        expected_raw = []
        exposures = np.array([.002, .004])
        for index, exposure in enumerate(exposures):
            sampled = np.random.default_rng(927 + index).poisson(expected_photo * exposure)
            expected_raw.append(np.rint(black + np.minimum(sampled, model["full_well_e"]) * scale))
        check("independent seeded charge and ADC reproduce actual HDR RAW",
              np.array_equal(raw, expected_raw))
        inferred_e = (raw.astype(float) - black) / scale
        inferred_rates = inferred_e / exposures[:, None, None]
        variance = (np.maximum(inferred_e, 0) + 1 / (12 * scale**2)) / exposures[:, None, None]**2
        inverse = 1 / variance
        direct_hdr = (inferred_rates * inverse).sum(axis=0) / inverse.sum(axis=0)
        check("HDR all fixture pixels usable", array(run, "hdr_valid_mask").all())
        check("HDR agrees with independent RAW-only inverse variance calculation", np.allclose(
            array(run, "hdr_merged_rate"), direct_hdr, rtol=3e-14, atol=1e-8))
        actual_means = [float((frame - black)[sites == site].mean())
                        for frame in raw.astype(float) for site in range(4)]
        check("prediction retains four sampled RAW site means",
              summary["prediction"]["values"] == actual_means)
        check("physical validation remains unestablished", summary["physical_validation"]
              == "not_established" and summary["uncertainty"]["kind"] == "unknown"
              and summary["simulation_oracle"]["used_by_hdr_merge"] is False)

        for label, record in (("psf", psf_job), ("ray", ray_job), ("spatial-run", spatial_run),
                              ("hdr-run", run)):
            for descriptor in record["artifacts"]:
                artifact(descriptor)
            if label.endswith("run"):
                package(label, record)
        for record in (sensor, parent, lens):
            for descriptor in record.get("response_artifacts", {}).values():
                artifact(descriptor)
        for label, operation in report["operations"].items():
            check("read back immutable identity " + label,
                  digest(client.get(operation["collection"] + "/" + operation["id"]))
                  == operation["record_sha256"])
        report.update(status="passed", records={"sensor": sensor["id"], "lens": lens["id"],
            "camera": camera["id"], "image": image_id, "scene_sequence": scene["id"],
            "control_sequence": control["id"], "capture_program": program["id"],
            "spatial_run": spatial_run["id"], "hdr_run": run["id"]},
            numeric={"expected_site_rate_min": float(expected_rate.min()),
                     "expected_site_rate_max": float(expected_rate.max()),
                     "mean_electrons_ratio": float(charge[1].sum() / charge[0].sum()),
                     "sampled_raw_site_means_dn": actual_means,
                     "raw_frame_count": int(raw.shape[0]), "raw_noise_enabled": True,
                     "exact_two_times_raw_claim": False})
        report.setdefault("successful_readback_times", []).append(datetime.now(UTC).isoformat())
        report.pop("last_failed_check", None)
        save()
        print(json.dumps({"status": "passed", "checks": len(report["checks"]),
                          "records": report["records"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
