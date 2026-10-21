"""Numerical model compression and exact optical-source admission tests."""

import copy
import hashlib
import io
import json

import numpy as np
import pytest

from camera_platform import calibration_db_optics_engine as engine
from camera_platform import calibration_lab_engine as lab


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _kernel(sx, sy, angle, shift=0):
    y, x = np.indices((33, 33), dtype=float)
    x, y = x - 16 - shift, y - 16
    a = np.radians(angle)
    u, v = x * np.cos(a) + y * np.sin(a), -x * np.sin(a) + y * np.cos(a)
    k = np.exp(-0.5 * ((u / sx) ** 2 + (v / sy) ** 2))
    return k / k.sum()


@pytest.fixture
def source(monkeypatch):
    profile = lab.default_profile()
    profile["pixel_pitch_um"] = 2.9
    wave = engine.WAVE
    white = np.linspace(1e16, 2e16, 21)
    response = np.linspace(0.2, 0.8, 21)[:, None] * np.array([0.8, 1, 1, 0.7])[None, :]
    psf = np.stack([np.stack([_kernel(1.5, 1, 25), _kernel(2, 1.3, -30)]) for _ in wave])
    arrays = {
        "psf": psf,
        "conditional_weights": np.ones((21, 2, 1)),
        "directions_xyz": np.array([[[0, 0, 1]], [[0, 0, 1]]]),
        "irradiance_per_radiance_sr": np.ones((21, 2)) * 0.03,
    }
    geometry = {
        "native_shape_row_col": [16, 16],
        "pixel_pitch_um": 2.9,
        "receiver_sampling": "pixel_integrated_optical_impulse",
        "source_sampling": "native_cell_center_with_optical_halo",
        "cfa_origin": {"pattern": "RGGB"},
    }
    fields = [
        {
            "id": name,
            "status": "succeeded",
            "geometry": copy.deepcopy(geometry),
            "x": xy,
            "y": xy,
            "image_xy_mm": [xy * 10, xy * 10],
            "azimuth_deg": angle,
            "normalized_radius": radius,
            "pupil_node_index": i,
        }
        for i, (name, xy, radius, angle) in enumerate(
            [("center", 0, 0, 0), ("outer-ne", 0.6, 0.85, 45)]
        )
    ]
    optical = {
        "schema": "camera_reference_workshop_optics_v1",
        "revision": "optical-test",
        "specification": {
            "optical_profile": "exit_pupil_diffraction",
            "wavelength_nm": wave.tolist(),
            "pixel_pitch_um": 2.9,
        },
        "fields": fields,
        "arrays_file": "optics.npz",
    }
    cell = {"pixel_pitch_um": 2.9, "cfa_pattern": "RGGB"}
    cell["cell_sha256"] = hashlib.sha256(_encoded(cell)).hexdigest()
    pixel = {
        "schema": "camera_design_workshop_absolute_qe_v1",
        "wavelength_nm": wave.tolist(),
        "theta_deg": [0, 60],
        "eqe": np.stack([response, response]).tolist(),
        "sites": list(engine.SITES),
        "qe_units": "electrons per sensor-plane incident photon",
        "cell_sha256": cell["cell_sha256"],
    }
    dc_value = np.sum(white[:, None] * response * 0.03 * 10 * (2.9e-6) ** 2, axis=0) * 0.18
    xyz = np.ones((24, 3)) * 0.3
    capture = {
        "configuration": {"luminance_cd_m2": 1000},
        "scene_reference": {"reference_xyz": xyz.tolist()},
    }
    payload = {
        "optical": optical,
        "pixel": pixel,
        "cell": cell,
        "arrays": arrays,
        "dc": {
            fid: {
                "capture": copy.deepcopy(capture),
                "uniform_planes": np.broadcast_to(dc_value, (16, 16, 4)).copy(),
            }
            for fid in ("center", "outer-ne")
        },
        "candidate": {
            "source_identities": {
                "lens": {"source": {"pupil_condition": {"effective_f_number": 4.12}}}
            }
        },
        "source_identity": {"run_id": "test", "optical_revision": "optical-test"},
    }
    spectral = {"white": white, "reference_xyz": xyz}
    monkeypatch.setattr(
        engine,
        "_spectral_evidence",
        lambda p: (spectral, {"original_source_spectrum_retained": False}, {}),
    )
    return payload, profile


def test_elliptical_fit_matches_actual_renderer_kernel():
    kernel = _kernel(1.7, 1.1, 37)
    result = engine.fit_gaussian(kernel)
    assert result["parameters"]["sigma_x_px"] == pytest.approx(1.7, rel=1e-3)
    assert result["parameters"]["sigma_y_px"] == pytest.approx(1.1, rel=1e-3)
    assert result["parameters"]["theta_deg"] == pytest.approx(37, abs=0.01)
    assert result["residuals"]["renderer_rmse"] < 1e-5
    assert result["residuals"]["max_abs"] < 0.002
    assert engine.FREQUENCY[-1] == 0.5


def test_otf_combines_phase_before_magnitude():
    left, right = _kernel(1.2, 1.2, 0, -2), _kernel(1.2, 1.2, 0, 2)
    combined = engine.transfer_mtf((left + right) / 2, np.array([0.125]), np.array([0.0]))
    average_magnitude = (
        engine.transfer_mtf(left, np.array([0.125]), np.array([0.0]))
        + engine.transfer_mtf(right, np.array([0.125]), np.array([0.0]))
    ) / 2
    assert combined[0, 0] < 1e-8
    assert average_magnitude[0, 0] > 0.6


def test_projection_is_center_only_and_preserves_profile_geometry(source):
    payload, profile = source
    before = copy.deepcopy(profile)
    result = engine.project_optical_profile(payload, profile)
    assert result["can_apply"] and result["status"] == "qualified"
    assert profile == before
    assert set(result["profile_patch"]) == {"blur", "conditions"}
    b = result["profile_patch"]["blur"]
    assert b["field_nodes"][0]["field_xy"] == [0, 0]
    assert len(b["field_nodes"]) == 1
    assert b["field_application"] == "explicit_nodes_only"
    assert b["included_effects"] == ["lens_blur", "pixel_aperture"]
    assert b["charge_diffusion_assumption"] == "zero_not_identified"
    assert result["report"]["fields"][1]["target_field_xy"] is None
    assert result["report"]["physical_validation"] == "not_established"
    assert not result["report"]["reverse_optical_replay_supported"]
    candidate = copy.deepcopy(profile)
    candidate["blur"] = b
    candidate["conditions"].update(result["profile_patch"]["conditions"])
    lab.validate_profile(candidate)


def test_explicit_field_mapping_has_no_lens_to_sensor_guess(source):
    payload, profile = source
    result = engine.project_optical_profile(
        payload, profile, field_mapping=[{"source_field_id": "outer-ne", "field_xy": [0.3, -0.4]}]
    )
    assert result["profile_patch"]["blur"]["field_nodes"][0]["field_xy"] == [0.3, -0.4]
    assert result["report"]["fields"][1]["source_field_xy"] == [0.6, 0.6]
    with pytest.raises(ValueError, match="normalized"):
        engine.project_optical_profile(
            payload, profile, field_mapping=[{"source_field_id": "outer-ne", "field_xy": [2, 0]}]
        )


def test_spectral_weights_use_pupil_qe_and_no_second_aperture(source):
    payload, profile = source
    result = engine.project_optical_profile(payload, profile)
    f = result["report"]["fields"][0]
    weights = np.asarray(f["spectral_weights_normalized_per_site"])
    expected = np.linspace(1e16, 2e16, 21) * np.linspace(0.2, 0.8, 21)
    assert np.allclose(weights[:, 1], expected / expected.sum())
    assert f["dc_parity"]["max_relative_error"] < 1e-12
    assert f["parameters"]["sigma_x_px"] == pytest.approx(1.5, rel=1e-3)
    assert f["parameters"]["sigma_y_px"] == pytest.approx(1, rel=1e-3)
    with np.load(
        io.BytesIO(result["derived_files"]["broadband-kernels.npz"]), allow_pickle=False
    ) as z:
        assert np.allclose(z["center_rgba_sites"][1], payload["arrays"]["psf"][0, 0])


def test_non_gaussian_kernel_is_retained_and_gaussian_is_only_a_rejected_diagnostic(source):
    payload, profile = source
    bad = 0.65 * _kernel(0.4, 0.4, 0) + 0.35 * _kernel(5, 5, 0)
    payload["arrays"]["psf"][:, 1] = bad
    assert engine.project_optical_profile(payload, profile)["can_apply"]
    result = engine.project_optical_profile(
        payload, profile, field_mapping=[{"source_field_id": "outer-ne", "field_xy": [0.5, 0.5]}]
    )
    assert result["can_apply"] and result["status"] == "qualified"
    assert result["report"]["gates"][-1]["status"] == "passed"
    assert not result["report"]["fields"][1]["gaussian_approximation"]["accepted"]
    assert result["report"]["fields"][1]["residuals"]["max_abs"] < 1e-12
    assert np.allclose(result["profile_patch"]["blur"]["field_nodes"][0]["kernel"], bad)
    assert result["report"]["fit_frequency_band_cy_px"] == [0, 0.5]


def test_sampled_runtime_transfer_mismatch_rejects(source, monkeypatch):
    payload, profile = source
    original = lab._effective_blur

    def wrong(image, node):
        if node.get("family") == "sampled_native_kernel_v1":
            return image[2::4, 2::4], _kernel(0.5, 0.5, 0)
        return original(image, node)

    monkeypatch.setattr(lab, "_effective_blur", wrong)
    result = engine.project_optical_profile(payload, profile)
    assert not result["can_apply"]
    assert result["report"]["gates"][-1]["status"] == "failed"


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda p, b: b.update(pixel_pitch_um=3.0), "pitch"),
        (
            lambda p, b: p["optical"]["fields"][0]["geometry"].update(receiver_sampling="point"),
            "sampling",
        ),
        (lambda p, b: p["pixel"].update(qe_units="relative"), "QE units"),
        (
            lambda p, b: p["dc"]["center"].update(
                uniform_planes=p["dc"]["center"]["uniform_planes"] * 1.01
            ),
            "DC parity",
        ),
        (
            lambda p, b: p["dc"]["center"]["capture"]["scene_reference"].update(
                reference_xyz=np.zeros((24, 3)).tolist()
            ),
            "XYZ",
        ),
    ],
)
def test_unsupported_or_inconsistent_source_rejected(source, mutation, match):
    payload, profile = source
    mutation(payload, profile)
    with pytest.raises(ValueError, match=match):
        engine.project_optical_profile(payload, profile)


def _write_tree(root, docs):
    root.mkdir(parents=True)
    for name, data in docs.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {
        "schema": "camera_reference_workshop_files_v1",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in docs.items()},
    }
    data = _encoded(manifest)
    (root / "manifest.json").write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _tree_documents(payload, run_id, conditional=False, ancestor_pin=None):
    prefix = (
        "conditions/condition-006/candidates/candidate-03/"
        if conditional
        else "candidates/candidate-03/"
    )
    stream = io.BytesIO()
    np.savez_compressed(stream, **payload["arrays"])
    optical = copy.deepcopy(payload["optical"])
    optical["arrays_sha256"] = hashlib.sha256(stream.getvalue()).hexdigest()
    candidate = {
        "id": "candidate-03",
        "status": "succeeded",
        "prefix": prefix,
        "source_identities": payload["candidate"]["source_identities"],
        "result": {"optical_revision": optical["revision"]},
    }
    if conditional:
        candidate["source_capture"] = {"run_id": "run-" + "a" * 32, "manifest_sha256": ancestor_pin}
    docs = {
        prefix + "result.json": _encoded(candidate),
        prefix + "sources.json": _encoded(candidate["source_identities"]),
        prefix + "optical-response.json": _encoded(optical),
        prefix + "pixel-cell.json": _encoded(payload["cell"]),
        prefix + "pixel-response.json": _encoded(payload["pixel"]),
    }
    if not conditional:
        docs[prefix + "optics/optics.npz"] = stream.getvalue()
    for fid, v in payload["dc"].items():
        stream = io.BytesIO()
        np.save(stream, v["uniform_planes"])
        data = stream.getvalue()
        capture = copy.deepcopy(v["capture"])
        capture["captures"] = {"uniform": {"planes_sha256": hashlib.sha256(data).hexdigest()}}
        docs[prefix + f"fields/{fid}/capture.json"] = _encoded(capture)
        docs[prefix + f"fields/{fid}/uniform/planes-electron-rate.npy"] = data
    summary = {
        "schema": "camera_db_design_study_result_v1",
        "id": run_id,
        "status": "succeeded",
        "comparison_mode": "aperture_conditions" if conditional else "aperture_sweep",
        "candidates": [candidate],
    }
    if conditional:
        condition = {"id": "condition-006", "candidates": [candidate]}
        summary["aperture_conditions"] = {"conditions": [condition]}
        docs["conditions/condition-006/result.json"] = _encoded(condition)
    docs["result.json"] = _encoded(summary)
    return docs


def test_missing_numerical_npz_resolves_only_pinned_ancestor_and_freezes_chain(source, tmp_path):
    payload, _ = source
    parent = tmp_path / ("run-" + "a" * 32)
    pin = _write_tree(parent, _tree_documents(payload, parent.name))
    child = tmp_path / ("run-" + "b" * 32)
    childpin = _write_tree(child, _tree_documents(payload, child.name, True, pin))
    loaded = engine.prepare_optical_source(child, None, "candidate-03", "condition-006", childpin)
    assert len(loaded["source_identity"]["ancestors"]) == 1
    assert loaded["manifest_bindings"][""] == childpin
    assert loaded["manifest_bindings"]["ancestors/" + parent.name + "/"] == pin
    assert (
        "ancestors/" + parent.name + "/candidates/candidate-03/optics/optics.npz"
        in loaded["frozen_files"]
    )
    frozen = tmp_path / "frozen"
    for name, data in loaded["frozen_files"].items():
        path = frozen / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert (
        engine.prepare_optical_source(frozen, None, "candidate-03", "condition-006", childpin)[
            "source_identity"
        ]
        == loaded["source_identity"]
    )
    (parent / "candidates/candidate-03/optics/optics.npz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash changed"):
        engine.prepare_optical_source(child, None, "candidate-03", "condition-006", childpin)


def test_omitted_summary_requires_manifest_pin(tmp_path):
    with pytest.raises(ValueError, match="manifest SHA256"):
        engine.prepare_optical_source(tmp_path, None, "candidate-03")


def test_reference_model_mapping_uses_only_retained_fields_and_explicit_domain(source):
    payload, profile = source
    original = copy.deepcopy(profile)
    result = engine.project_optical_profile(payload, profile, field_mapping_mode="reference_model")
    blur = result["profile_patch"]["blur"]
    assert profile == original
    assert blur["field_coordinate_domain"] == "reference_lens_object_direction"
    assert blur["field_mapping_mode"] == "reference_model"
    assert len(blur["field_nodes"]) == 2
    assert [n["field_xy"] for n in blur["field_nodes"]] == [[0, 0], [0.6, 0.6]]
    assert [n["source_field_id"] for n in blur["field_nodes"]] == ["center", "outer-ne"]
    assert all(f["applied"] for f in result["report"]["fields"])
    assert all(
        f["target_field_coordinate_domain"] == "reference_lens_object_direction"
        for f in result["report"]["fields"]
    )
    assert result["report"]["applicable_fields"][1] == {
        "source_field_id": "outer-ne",
        "field_xy": [0.6, 0.6],
        "field_coordinate_domain": "reference_lens_object_direction",
    }
    # An optical field without preserved DC/radiometry evidence is not invented
    # as an applicable node; neither are mirrored corners of the one outer field.
    del payload["dc"]["outer-ne"]
    limited = engine.project_optical_profile(payload, profile, field_mapping_mode="reference_model")
    assert len(limited["profile_patch"]["blur"]["field_nodes"]) == 1


@pytest.mark.parametrize("mode", ["sensor", "all", "center_only", True])
def test_unknown_mapping_mode_is_rejected(source, mode):
    payload, profile = source
    with pytest.raises(ValueError, match="field_mapping_mode"):
        engine.project_optical_profile(payload, profile, field_mapping_mode=mode)


def test_reference_mode_cannot_be_combined_with_explicit_sensor_mapping(source):
    payload, profile = source
    with pytest.raises(ValueError, match="mutually exclusive"):
        engine.project_optical_profile(
            payload,
            profile,
            field_mapping_mode="reference_model",
            field_mapping=[{"source_field_id": "center", "field_xy": [0, 0]}],
        )


def test_legacy_and_explicit_sensor_mapping_keep_their_domain(source):
    payload, profile = source
    default = engine.project_optical_profile(payload, profile)
    assert default["profile_patch"]["blur"]["field_coordinate_domain"] == "sensor_normalized"
    assert default["report"]["field_mapping_mode"] == "center_only"
    explicit = engine.project_optical_profile(
        payload, profile, field_mapping=[{"source_field_id": "outer-ne", "field_xy": [0.2, 0.4]}]
    )
    assert explicit["profile_patch"]["blur"]["field_coordinate_domain"] == "sensor_normalized"
    assert explicit["report"]["field_mapping_mode"] == "explicit_sensor"


def test_peripheral_reference_kernel_reaches_fresh_runtime_without_sensor_relabel(source, tmp_path):
    payload, profile = source
    conversion = engine.project_optical_profile(
        payload, profile, field_mapping_mode="reference_model"
    )
    profile["blur"] = conversion["profile_patch"]["blur"]
    profile["conditions"].update(conversion["profile_patch"]["conditions"])
    node = profile["blur"]["field_nodes"][1]
    frequency = np.linspace(0, 0.5, 33)
    record = {
        "id": "outer-reference-model",
        "chart": "mtf",
        "source_kind": "synthetic",
        "measurement_domain": "camera_raw",
        "role": "training",
        "conditions": copy.deepcopy(profile["conditions"]),
        "observations": {
            "frequency_cy_px": frequency.tolist(),
            "mtf": lab._mtf(node, frequency, 45).tolist(),
            "orientation_deg": 45.0,
            "field_xy": [0.6, 0.6],
            "field_coordinate_domain": "reference_lens_object_direction",
        },
    }
    result = lab.run_comparison(profile, profile, [record], tmp_path / "comparison", seed=7)
    assert result["comparisons"][0]["candidate"] == result["comparisons"][0]["measured"]
    assert result["comparisons"][0]["field_xy"] == [0.6, 0.6]
    assert result["comparisons"][0]["field_coordinate_domain"] == "reference_lens_object_direction"
    for image in result["images"]:
        assert image["blur_field"]["source_field_id"] == "outer-ne"
        assert image["blur_field"]["field_coordinate_domain"] == "reference_lens_object_direction"
        assert image["blur_field"]["selection"] == "declared_observation"
        folder = tmp_path / "comparison" / image["raw"]
        applied = np.load(folder.parent / "effective-kernel.npy")
        np.testing.assert_array_equal(applied, node["kernel"])
    raw_record = copy.deepcopy(record)
    raw_record["quality"] = {"method": "native_bayer_mtf_v1"}
    with pytest.raises(ValueError, match="Native RAW field coordinates"):
        lab.fit_profile(profile, [raw_record])


@pytest.mark.parametrize("require_exact", [True, False])
def test_reference_field_missing_wrong_domain_and_unknown_position_never_fall_back(
    source, require_exact
):
    payload, profile = source
    converted = engine.project_optical_profile(
        payload, profile, field_mapping_mode="reference_model"
    )
    profile["blur"] = converted["profile_patch"]["blur"]
    lab.validate_profile(profile)
    for observations in (
        {"field_xy": [0.6, 0.6]},
        {"field_xy": [0.6, 0.6], "field_coordinate_domain": "sensor_normalized"},
    ):
        with pytest.raises(ValueError, match="field_coordinate_domain"):
            lab._field_node(profile, observations, require_exact=require_exact)
    for xy in ([0.2, 0.2], [-0.6, -0.6], [0.6, -0.6]):
        with pytest.raises(ValueError, match="No effective PSF"):
            lab._field_node(
                profile,
                {"field_xy": xy, "field_coordinate_domain": "reference_lens_object_direction"},
                require_exact=require_exact,
            )


def test_reference_domain_does_not_block_electrical_or_nonspatial_probe(source, tmp_path):
    payload, profile = source
    converted = engine.project_optical_profile(
        payload, profile, field_mapping_mode="reference_model"
    )
    profile["blur"] = converted["profile_patch"]["blur"]
    records = []
    for chart, observations in (
        ("dark", {"exposure_s": [0.01, 0.1], "mean_dn": [64, 64], "variance_dn2": [1, 1]}),
        ("tone", {"input_level": [0, 0.25, 0.5, 1], "output_level": [0, 0.25, 0.5, 1]}),
    ):
        records.append(
            {
                "id": chart + "-reference-domain",
                "chart": chart,
                "source_kind": "synthetic",
                "measurement_domain": "camera_isp" if chart == "tone" else "camera_raw",
                "role": "training",
                "conditions": copy.deepcopy(profile["conditions"]),
                "observations": observations,
            }
        )
    result = lab.run_comparison(profile, profile, records, tmp_path / "nonspatial", seed=7)
    assert len(result["comparisons"]) == 2
    for image in result["images"]:
        if image["chart"] == "dark":
            assert image["blur_field"] is None
        else:
            assert image["blur_field"]["source_field_id"] == "center"
            assert image["blur_field"]["selection"] == "center_probe"
            assert (
                image["blur_field"]["field_coordinate_domain"] == "reference_lens_object_direction"
            )
