"""Physical invariants for the optional RayOptics PSF asset builder."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np
import pytest

pytest.importorskip("rayoptics")
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("lens_psf_builder_test", ROOT / "tools/build_lens_patent_raytrace_psf_grid.py")
builder = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = builder
spec.loader.exec_module(builder)


@pytest.fixture
def singlet():
    """Explicit thick symmetric singlet, stop, and image plane in mm."""
    candidate = builder.Candidate(
        simulation_id="synthetic:infinity", lens_id="synthetic", company="Test",
        publication_number="synthetic", example_label="thick singlet", configuration="infinity",
        f_number=10., focal_length_mm=50.85, image_height_mm=1., half_field_deg=None,
        source_surfaces_csv="synthetic.csv", optics_json={},
    )
    surfaces = [
        dict(surface_order="1", surface_label="stop", surface_kind="stop", radius="Infinity", thickness="0", effective_aperture="6", nd="", vd=""),
        dict(surface_order="2", surface_label="1", surface_kind="surface", radius="50", thickness="5", effective_aperture="20", nd="1.5", vd="50"),
        dict(surface_order="3", surface_label="2", surface_kind="surface", radius="-50", thickness="49.15", effective_aperture="20", nd="", vd=""),
        dict(surface_order="4", surface_label="image", surface_kind="image", radius="Infinity", thickness="0", effective_aperture="20", nd="", vd=""),
    ]
    return candidate, surfaces


def bundle(candidate, surfaces, **kwargs):
    options = dict(wavelengths_nm=np.array([550.]), field_count=3, pupil_samples=13,
                   psf_size=64, sample_spacing_um=None, min_success_fraction=.25)
    options.update(kwargs)
    return builder.build_raytrace_psf_bundle(candidate, surfaces, **options)


def test_spectral_sampling_keeps_physical_field_and_pupil_fixed(singlet):
    candidate, surfaces = singlet
    one = bundle(candidate, surfaces)
    multi = bundle(candidate, surfaces, wavelengths_nm=np.array([650., 450., 550.]))
    assert multi.wavelength_nm.tolist() == [450., 550., 650.]
    np.testing.assert_allclose(one.geometry_function[:, 0], multi.geometry_function[:, 1], atol=1e-12)
    np.testing.assert_allclose(one.chief_direction_cosines[:, 0], multi.chief_direction_cosines[:, 1], atol=1e-12)
    assert one.quality["entrance_pupil_radius_mm"] == pytest.approx(multi.quality["entrance_pupil_radius_mm"], abs=1e-12)
    model, _ = builder.build_rayoptics_model(candidate, surfaces, 3, np.array([450., 650.]))
    assert model["optical_spec"]["wvls"].central_wvl == 550.


def test_directions_and_conditional_energy_have_explicit_contract(singlet):
    candidate, surfaces = singlet
    traced = bundle(candidate, surfaces)
    admitted = traced.pupil_weights > 0
    np.testing.assert_allclose(np.linalg.norm(traced.image_direction_cosines[admitted], axis=-1), 1., atol=1e-12)
    np.testing.assert_allclose(traced.pupil_weights.sum(axis=-1), 1.)
    np.testing.assert_allclose(traced.psf_function.sum(axis=(0, 1)), 1.)
    assert np.all(traced.retained_energy_fraction >= .999)
    assert np.all(traced.chief_points_mm[1:, :, 0] > 0)
    np.testing.assert_allclose(traced.chief_points_mm[:, :, 1], 0., atol=1e-12)
    np.testing.assert_allclose(traced.chief_ray_angle_deg,
        np.degrees(np.arctan2(np.linalg.norm(traced.chief_direction_cosines[:, :, :2], axis=-1), np.abs(traced.chief_direction_cosines[:, :, 2]))))
    meta = builder.build_raytrace_optics_json(candidate, traced, "synthetic.npz")
    rt = meta["raytrace"]
    assert rt["contract_version"] == 2
    assert rt["psf_reference_axis"] == "+x" and rt["row_axis"] == "y_up"
    assert rt["radiometry_status"] == "unknown"
    assert rt["psf_kind"] == "geometric" and rt["diffraction_included"] is False
    assert meta["transmittance"]["status"] == "unknown"
    assert len(rt["source_prescription_sha256"]) == len(rt["generator_sha256"]) == 64


def test_real_stop_clips_rays_without_faking_relative_illumination(singlet):
    candidate, surfaces = singlet
    surfaces[0]["effective_aperture"] = "3.5"
    traced = bundle(candidate, surfaces)
    assert np.all(traced.success_counts < traced.requested_counts)
    assert np.all(traced.pupil_throughput_fraction < 1.)
    assert np.isnan(traced.image_direction_cosines[traced.pupil_weights == 0]).all()
    # RI is explicitly unknown, not substituted with clipping survival counts.
    np.testing.assert_array_equal(traced.relative_illumination_function, 1.)


def test_nonzero_coefficients_override_legacy_flag_and_support_odd_orders():
    from rayoptics.elem.surface import Surface
    ifc = Surface()
    builder.apply_asphere(ifc, dict(is_aspheric="0", conic="-1.0302", a4=".018188", a18=".002295"))
    assert type(ifc.profile).__name__ == "EvenPolynomial"
    assert ifc.profile.coefs[1] == .018188
    assert ifc.profile.coefs[8] == .002295
    odd = Surface()
    builder.apply_asphere(odd, dict(is_aspheric="0", a3=".001", a4=".002"))
    assert type(odd.profile).__name__ == "RadialPolynomial"
    assert odd.profile.coefs[2] == .001
    with pytest.raises(ValueError, match="Unsupported"):
        builder.apply_asphere(Surface(), dict(a22=".001"))


@pytest.mark.parametrize("field,value", [("radius", "unknown"), ("thickness", "(Variable)"), ("thickness", "K"), ("thickness", "d15")])
def test_unresolved_values_do_not_become_zero(singlet, field, value):
    candidate, surfaces = singlet
    surfaces[1][field] = value
    with pytest.raises(ValueError, match="validation|unresolved"):
        bundle(candidate, surfaces)


def test_invalid_model_and_unknown_aperture_are_rejected(singlet):
    candidate, surfaces = singlet
    surfaces[1]["effective_aperture"] = ""
    with pytest.raises(ValueError, match="clear aperture"):
        bundle(candidate, surfaces)
    surfaces[1]["effective_aperture"] = "20"
    candidate = builder.Candidate(**{**candidate.__dict__, "focal_length_mm": 2.6})
    with pytest.raises(ValueError, match="EFL"):
        bundle(candidate, surfaces)


def test_finite_conjugate_requires_explicit_distance(singlet):
    candidate, surfaces = singlet
    candidate = builder.Candidate(**{**candidate.__dict__, "configuration": "1m"})
    with pytest.raises(ValueError, match="object-distance"):
        bundle(candidate, surfaces)
    model, _ = builder.build_rayoptics_model(candidate, surfaces, 3, np.array([550.]), object_distance_m=1.)
    assert model["seq_model"].gaps[0].thi == 1000.


def test_pixel_centers_keep_signed_centroid_and_never_create_failure_delta():
    points = np.array([[1.2, .7], [1.2, .7]])
    k = builder.histogram_psf(points, 8, 1.)
    rows, cols = np.indices(k.shape)
    assert ((cols - 3.5) * k).sum() == pytest.approx(1.2)
    assert ((3.5 - rows) * k).sum() == pytest.approx(.7)
    centered = builder.histogram_psf(np.array([[0.,0.]]), 8, 1.)
    assert np.count_nonzero(centered) == 4
    with pytest.raises(ValueError, match="without image"):
        builder.histogram_psf(np.empty((0, 2)), 8, 1.)
    with pytest.raises(ValueError, match="retains only"):
        builder.histogram_psf(np.array([[100.,100.]]), 8, 1.)


def test_sensor_sampling_gate_is_independent_of_grid_label(singlet):
    candidate, surfaces = singlet
    with pytest.raises(ValueError, match="sensor pitch"):
        bundle(candidate, surfaces, sample_spacing_um=20., sensor_pitch_um=2.)


def test_image_labeled_exit_face_retains_refraction_and_matches_lensmaker(singlet):
    """An exit face mislabeled 'image' must not leave the sensor in glass."""
    from opticalglass.modelglass import ModelGlass
    candidate, surfaces = singlet
    surfaces[2]["surface_kind"] = "image"
    model, _ = builder.build_rayoptics_model(candidate, surfaces, 3, np.array([550.]))
    index = ModelGlass(1.5, 50., "synthetic").rindex(550.)
    expected_efl = 1 / ((index - 1) * (1 / 50. - 1 / -50. + (index - 1) * 5 / (index * 50 * -50)))
    actual_efl = model["analysis_results"]["parax_data"].fod.efl
    assert actual_efl == pytest.approx(expected_efl, rel=1e-10)
    assert model["seq_model"].gaps[-1].medium.rindex(550.) == 1.


def test_existing_asset_does_not_bypass_source_preflight(tmp_path, monkeypatch, singlet):
    import argparse
    import csv
    candidate, surfaces = singlet
    surfaces[1]["thickness"] = "(Variable)"
    source = tmp_path / "synthetic.csv"
    with source.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=surfaces[0].keys())
        writer.writeheader()
        writer.writerows(surfaces)
    out = tmp_path / "out"
    out.mkdir()
    existing = out / "synthetic_infinity.npz"
    existing.write_bytes(b"legacy asset is preserved")
    args = argparse.Namespace(
        list_presets=False, out_dir=out, export_root=tmp_path, camerae2e_db=tmp_path / "db.sqlite",
        reset_manifest=False, overwrite=False, preset="debug", field_count=3,
        wavelengths="550", pupil_samples=13, psf_size=64, sample_spacing_um=None,
        min_success_fraction=.25, reference_wavelength_nm=550., object_distance_m=float("inf"),
        efl_relative_tolerance=.05, min_retained_energy=.999, sensor_pitch_um=None,
    )
    monkeypatch.setattr(builder, "parse_args", lambda: args)
    monkeypatch.setattr(builder, "load_candidates", lambda args: [candidate])
    assert builder.main() == 1
    assert existing.read_bytes() == b"legacy asset is preserved"
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["rows"][0]["status"] == "failed"
    assert "unresolved_thickness" in manifest["rows"][0]["error"]


def test_real_v9_asphere_and_impossible_asset_regressions():
    database = ROOT / "camerae2e_db/lens_db/CameraE2E_Lens_DB_v9_20260627/data/lens_patents/lens_patent_simulation_v9.sqlite"
    exports = Path.home() / "Lens_Patent_DB/exports/prescriptions_expanded_v9"
    if not database.exists() or not exports.exists():
        pytest.skip("External patent source fixtures are not installed.")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        for sid in ("p0102:base", "p0247:base"):
            row = con.execute("SELECT * FROM simulation_results WHERE simulation_id=?", (sid,)).fetchone()
            candidate = builder.Candidate(**{k:json.loads(row[k]) if k == "optics_json" else row[k] for k in builder.Candidate.__dataclass_fields__})
            surfaces = builder.read_csv(exports / row["source_surfaces_csv"])
            if sid == "p0102:base":
                from rayoptics.elem.surface import Surface
                ifc = Surface()
                builder.apply_asphere(ifc, surfaces[0])
                assert surfaces[0]["is_aspheric"] == "0"
                assert type(ifc.profile).__name__ == "EvenPolynomial"
                assert ifc.profile.coefs[1] == .018188
            else:
                with pytest.raises(ValueError, match="validation"):
                    bundle(candidate, surfaces)
