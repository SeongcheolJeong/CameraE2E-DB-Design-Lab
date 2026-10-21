"""Opt-in spectral transport keeps the historical Reference calculations intact."""

import copy
import json

import numpy as np
import pytest

from camera_platform import reference_ircf_optics as ircf
from camera_platform import reference_workshop_optics as optics


def _solution(step=10):
    spec = ircf.specification(step)
    wave = np.asarray(spec["wavelength_nm"])
    field = optics.field_layout()[0]
    field.update(
        status="succeeded",
        issues=[],
        geometry={
            "input_shape_row_col": [8, 8],
            "crop_bounds_row_col": [2, 6, 2, 6],
        },
    )
    psf = np.zeros((len(wave), 1, 3, 3))
    psf[..., 1, 1] = 1
    directions = np.tile([[0, 0, 1], [0.5, 0, np.sqrt(0.75)]], (3, 1, 1))
    return {
        "result": {"schema": ircf.SCHEMA, "revision": "test", "fields": [field]},
        "arrays": {
            "wavelength_nm": wave,
            "psf": psf,
            "spectral_bin_width_nm": optics.spectral_widths(wave),
            "irradiance_per_radiance_sr": np.ones((len(wave), 3)) * 0.02,
            "conditional_weights": np.tile([0.25, 0.75], (len(wave), 3, 1)),
            "directions_xyz": directions,
            "directions_xyz_per_wavelength": np.tile(directions, (len(wave), 1, 1, 1)),
        },
    }


def test_extended_axis_is_explicit_and_legacy_default_is_unchanged():
    original = optics.template()
    assert original["schema"] == optics.SCHEMA
    assert original["wavelength_nm"] == list(range(450, 651, 10))
    for step, length in ((10, 61), (5, 121)):
        spec = ircf.specification(step)
        assert len(spec["wavelength_nm"]) == length
        assert optics.normalize(spec) == spec
        assert spec["kernel_size"] == 257 and original["kernel_size"] == 129
        assert optics.geometry(spec)["input_shape_row_col"] == [512, 512]
        assert optics.spectral_widths(spec["wavelength_nm"]).sum() == 600
    assert optics.template() == original
    for step in (True, 0, 20, 2.5):
        with pytest.raises(ValueError, match="step"):
            ircf.specification(step)
    extended = ircf.specification()
    extended["schema"] = optics.SCHEMA
    with pytest.raises(ValueError, match="450"):
        optics.normalize(extended)


def test_constant_transmission_energy_and_optional_group_planes():
    solution = _solution()
    wave = solution["arrays"]["wavelength_nm"]
    photons = np.ones((8, 8, len(wave))) * 1e12
    full = optics.apply_field(solution, "center", photons, pixel_qe=np.ones((len(wave), 4)))
    output = optics.apply_field(
        solution,
        "center",
        photons,
        angular_response=lambda theta, phi, wave: np.full((len(theta), 4), 0.2),
        return_spectral=True,
        spectral_groups={"vis": wave <= 700, "nir": wave > 700},
    )
    np.testing.assert_allclose(full["mean_electron_rate"], 1e12 * 600 * 0.02 * (2.9e-6) ** 2)
    np.testing.assert_allclose(output["mean_electron_rate"], full["mean_electron_rate"] * 0.2)
    groups = output["group_planes_electron_rate"]
    np.testing.assert_allclose(groups["vis"] + groups["nir"], output["planes_electron_rate"])
    np.testing.assert_allclose(
        output["spectral_planes_electron_rate"].sum(axis=0), output["planes_electron_rate"]
    )
    np.testing.assert_allclose(
        output["center_electron_rate_per_band"], output["spectral_planes_electron_rate"][:, 2, 2]
    )
    assert "spectral_planes_electron_rate" not in full
    assert output["metadata"]["pixel_aperture_applications"] == 1


def test_aoi_uses_each_wave_sensor_directions_and_never_renormalizes_filter_loss():
    solution = _solution()
    directions = solution["arrays"]["directions_xyz_per_wavelength"]
    directions[1, :, 1] = [np.sqrt(0.75), 0, 0.5]  # 60 degrees, not old550nm30 degrees.

    def response(theta, phi, wave):
        return np.repeat((np.cos(np.deg2rad(theta)) ** 2 * 0.4)[:, None], 4, axis=1)

    effective = ircf.effective_response(solution, "center", response)
    np.testing.assert_allclose(effective[0], 0.4 * (0.25 + 0.75 * 0.75))
    np.testing.assert_allclose(effective[1], 0.4 * (0.25 + 0.75 * 0.25))
    theta, _, weights = ircf.pupil_samples(solution, "center", 1)
    np.testing.assert_allclose(theta, [0, 60])
    np.testing.assert_array_equal(weights, [0.25, 0.75])
    wave = solution["arrays"]["wavelength_nm"]
    output = optics.apply_field(
        solution, "center", np.ones((8, 8, len(wave))), angular_response=response
    )
    expected = (
        effective * solution["arrays"]["spectral_bin_width_nm"][:, None] * 0.02 * (2.9e-6) ** 2
    )
    np.testing.assert_allclose(output["center_electron_rate_per_band"], expected)


def test_refinement_keeps_same_interval_and_zero_transmission_has_no_photoelectrons():
    totals = []
    for step in (10, 5):
        solution = _solution(step)
        wave = solution["arrays"]["wavelength_nm"]
        photons = np.broadcast_to(wave, (8, 8, len(wave)))
        output = optics.apply_field(solution, "center", photons, pixel_qe=np.ones((len(wave), 4)))
        totals.append(output["mean_electron_rate"])
        zero = optics.apply_field(solution, "center", photons, pixel_qe=np.zeros((len(wave), 4)))
        assert np.count_nonzero(zero["mean_electron_rate"]) == 0
    np.testing.assert_allclose(*totals, rtol=1e-14)


def test_rejects_changed_integration_weights_and_unnamed_spectral_masks():
    solution = _solution()
    wave = solution["arrays"]["wavelength_nm"]
    photons = np.ones((8, 8, len(wave)))
    bad = copy.deepcopy(solution)
    bad["arrays"]["spectral_bin_width_nm"][0] = 10
    with pytest.raises(ValueError, match="weights"):
        optics.apply_field(bad, "center", photons, pixel_qe=np.ones((len(wave), 4)))
    with pytest.raises(ValueError, match="boolean"):
        optics.apply_field(
            solution,
            "center",
            photons,
            pixel_qe=np.ones((len(wave), 4)),
            spectral_groups={"bad": list(range(len(wave)))},
        )


def test_new_cache_namespace_and_exact_step_are_forwarded(monkeypatch, tmp_path):
    recorded = {}
    monkeypatch.setattr(optics, "cache_key", lambda workspace, spec: spec["schema"])
    monkeypatch.setattr(
        optics,
        "solve",
        lambda workspace, spec, folder, **kw: recorded.update(
            spec=spec, folder=folder, callback=kw["on_progress"]
        ),
    )
    monkeypatch.setattr(optics, "load_solution", lambda folder: {"test": str(folder)})

    def callback(text):
        pass

    result, folder = ircf.solve(tmp_path, tmp_path, step_nm=5, on_progress=callback)
    assert folder == tmp_path / "ircf-optics" / ircf.SCHEMA
    assert result == {"test": str(folder)}
    assert recorded["callback"] is callback
    assert recorded["spec"]["wavelength_nm"] == list(range(400, 1001, 5))


def test_expanded_support_reuses_only_hash_verified_intermediates(monkeypatch, tmp_path):
    monkeypatch.setattr(optics, "cache_key", lambda workspace, spec: str(spec["kernel_size"]))
    source = tmp_path / "ircf-optics/129"
    source.mkdir(parents=True)
    raw = source / "geometric-fixture.npz"
    np.savez_compressed(raw, actual_ray_points=np.array([[0.0, 0.28]]))
    (source / "geometric-fixture.json").write_text(
        json.dumps({"sha256": optics.shared._file_sha(raw)})
    )
    (source / "optics.npz").write_bytes(b"old clipped PSF must not be copied")
    spec = ircf.specification()
    (source / "result.json").write_text(json.dumps({"specification": {**spec, "kernel_size": 129}}))
    target = tmp_path / "ircf-optics/257"
    ircf._reuse_smaller_support(tmp_path, tmp_path, spec, target)
    assert (target / raw.name).read_bytes() == raw.read_bytes()
    assert not (target / "optics.npz").exists()
    assert not (target / "result.json").exists()
    assert (
        json.loads((target / "support-expansion.json").read_text())["psf_or_result_reused"] is False
    )
    raw.write_bytes(b"modified ray cache")
    with pytest.raises(ValueError, match="array hash changed"):
        ircf._reuse_smaller_support(tmp_path, tmp_path, spec, target)


@pytest.mark.parametrize("extended", [False, True])
def test_solve_assembly_uses_exact_axis_and_preserves_550nm_field_reference(
    monkeypatch,
    tmp_path,
    extended,
):
    spec = ircf.specification(5) if extended else optics.template()
    spec["kernel_size"] = 17
    wave = np.asarray(spec["wavelength_nm"])
    picture_calls = []
    monkeypatch.setattr(optics, "source_metadata", lambda workspace: {"prescription_sha256": "x"})
    monkeypatch.setattr(optics, "cache_key", lambda workspace, spec: "cache")
    monkeypatch.setattr(optics, "_backend", lambda: {})
    monkeypatch.setattr(
        optics,
        "_request",
        lambda workspace, spec, waves: {
            "prescription": {"wavelength_nm": waves, "surfaces": [], "sampling": {}}
        },
    )
    monkeypatch.setattr(optics, "_wavefront_diagnostic", lambda request, radius: {})

    def rays(request, wi, radii, samples):
        return [
            {
                "points": np.array([[0.0, radius]]),
                "directions": np.array([[0, 0, 1.0]]),
                "chief": np.array([0.0, radius]),
                "chief_direction": np.array([0, 0, 1.0]),
                "requested_rays": 1,
            }
            for radius in radii
        ]

    def batch(directory, label, request, backend, characterize, callback):
        count = len(request["wavelength_nm"])
        return {"status": "PROPOSED"}, {
            "irradiance_per_radiance_sr": np.ones((count, 3)) * 0.02,
            "directional_conditional_weights": np.ones((count, 3, 1)),
            "image_direction_cosines": np.tile([[[0.0, 0.0, 1.0]]], (3, 1, 1)),
        }

    def picture(directory, reference, fields, psf, reference_index):
        picture_calls.append(reference_index)
        return {}

    monkeypatch.setattr(optics, "_rays", rays)
    monkeypatch.setattr(optics.shared, "_batch", batch)
    monkeypatch.setattr(optics, "_picture", picture)
    result = optics.solve(tmp_path, spec, tmp_path / "optics")
    loaded = optics.load_solution(tmp_path / "optics")
    assert result["schema"] == spec["schema"]
    assert len(result["fields"]) == 9
    assert all(field["status"] == "succeeded" for field in result["fields"])
    assert loaded["arrays"]["psf"].shape == (len(wave), 9, 17, 17)
    assert picture_calls == [spec["wavelength_nm"].index(550.0)]
    assert ("directions_xyz_per_wavelength" in loaded["arrays"]) == extended
    if extended:
        assert loaded["arrays"]["directions_xyz_per_wavelength"].shape == (len(wave), 3, 1, 3)
        assert loaded["arrays"]["spectral_bin_width_nm"].sum() == 600
    else:
        assert "spectral_bin_width_nm" not in loaded["arrays"]
