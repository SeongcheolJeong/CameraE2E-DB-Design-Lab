"""The platform can read out source-computed charge without spectral assets."""

import numpy as np
import pytest

from camera_platform.precomputed_raw import capture_precomputed_raw
from pyisetcam.sensor import sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor, resample_sensor_capture


def test_charge_only_readout_matches_existing_bayer_capture(asset_store, monkeypatch):
    from pyisetcam.assets import AssetStore

    wave = np.array([450.0, 550.0, 650.0])
    charge = np.arange(64, dtype=float).reshape(8, 8) * 30.0
    readout = {
        "gain": 1.5, "full_well_e": 9000.0, "read_noise_e": 2.0,
        "dark_current_e_per_s": 3.0, "bit_depth": 12, "black_level_dn": 64.0,
    }
    ordinary = sensor_create(
        "bayer-rggb", {"size": charge.shape, "wave": wave}, asset_store=asset_store
    )
    ordinary = sensor_set(ordinary, "pixel size", 3.0e-6)
    ordinary = sensor_set(ordinary, "integration time", 0.01)
    ordinary = sensor_set(ordinary, "auto exposure", False)
    ordinary = sensor_set(ordinary, "noise flag", 2)
    ordinary.fields["pixel"].update(prnu_sigma=0.0, dsnu_sigma_v=0.0)
    ordinary.fields["column_fpn"] = [0.0, 0.0]
    ordinary = configure_e2e_sensor(ordinary, readout=readout)
    ordinary.data["mean_electrons"] = charge
    expected = resample_sensor_capture(ordinary, seed=42)

    def no_implicit_asset_store(*_args, **_kwargs):
        raise AssertionError("Charge-only RAW capture must not load upstream assets.")

    monkeypatch.setattr(AssetStore, "default", no_implicit_asset_store)
    frames, reference, metadata = capture_precomputed_raw(
        charge, wavelength_nm=wave, pixel_pitch_m=3.0e-6, exposure_s=0.01,
        cfa_phase="rggb", readout=readout, noise_enabled=True, frames=1, seed=42,
    )
    np.testing.assert_array_equal(frames[0], expected.data["raw_dn"])
    np.testing.assert_array_equal(reference, expected.data["reference_dn"])
    assert metadata[0] == expected.metadata["sensor_capture"]


@pytest.mark.parametrize("phase", ["rggb", "grbg", "gbrg", "bggr"])
def test_charge_only_readout_accepts_all_bayer_crop_phases(phase, monkeypatch):
    from pyisetcam.assets import AssetStore

    monkeypatch.setattr(
        AssetStore, "default",
        lambda *_args, **_kwargs: pytest.fail("Unused color filters were loaded"),
    )
    frames, reference, _ = capture_precomputed_raw(
        np.full((4, 6), 100.0), wavelength_nm=[550.0], pixel_pitch_m=3.0e-6,
        exposure_s=0.01, cfa_phase=phase,
        readout={"gain": 1.0, "full_well_e": 10000.0, "read_noise_e": 0.0,
                 "dark_current_e_per_s": 0.0, "bit_depth": 12, "black_level_dn": 64.0},
        noise_enabled=False, frames=2, seed=1,
    )
    assert frames.shape == (2, 4, 6)
    np.testing.assert_array_equal(frames[0], frames[1])
    assert reference.shape == (4, 6)


def test_charge_only_readout_rejects_invalid_precomputed_charge():
    with pytest.raises(ValueError, match="finite nonnegative"):
        capture_precomputed_raw(
            np.array([[np.nan]]), wavelength_nm=[550.0], pixel_pitch_m=3.0e-6,
            exposure_s=0.01, cfa_phase="rggb", readout={},
            noise_enabled=False, frames=1, seed=1,
        )
