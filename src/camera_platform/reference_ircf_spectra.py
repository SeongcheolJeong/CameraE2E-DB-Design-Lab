"""Explicit visible/NIR inputs for the Reference IRCF comparison.

The visible branch reuses the reviewed structural reference.  Its materials
are NOT extended into the NIR: that branch is a separately declared absolute
QE sensitivity model.  Reflectance and light tables are never extrapolated.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import numpy as np
from scipy.constants import c, h
from scipy.io import loadmat

from . import reference_workshop_sensor as reference_pixel
from .offline_assets import offline_asset_identity, offline_asset_store
from .registry import digest

SCHEMA = "camera_reference_ircf_spectra_v1"
PIXEL_SCHEMA = "camera_reference_ircf_hybrid_pixel_v1"
CMF_URL = "https://files.cie.co.at/Publications-datasets/CIE_xyz_1931_2deg.csv"
CMF_SOURCE_SHA256 = "fa663e3535a7e0763a745993a1f0a192eb0275ac46ad2d1befd7626841e713c1"
_FILES = {
    "baseline_filter": "data/sensor/irfilters/ircf_public.mat",
    "schott_filter": "data/sensor/irfilters/irFilter_schott.mat",
    "day": "data/lights/visible_ir/ill_DAY.mat",
    "ill_a": "data/lights/visible_ir/ill_A.mat",
    "d65": "data/lights/D65.mat",
    "macbeth": "data/surfaces/reflectances/macbethChart.mat",
    "munsell": "data/surfaces/reflectances/MunsellSamples_Vhrel.mat",
}

# CIE 2019, DOI 10.25039/CIE.DS.xvudnb9b, CC BY-SA 4.0.
# Data-only adaptation: retain every fifth 400..830 nm row of the official
# 1 nm CSV.  Source checksum above; interpolation is linear, outside830 zero.
# This table is separate from the Python implementation and retains attribution
# and the data's https://creativecommons.org/licenses/by-sa/4.0/ terms.
_CMF_ROWS = """400,0.014310000000,0.0003960000000,0.067850010000
405,0.023190000000,0.0006400000000,0.110200000000
410,0.043510000000,0.0012100000000,0.207400000000
415,0.077630000000,0.0021800000000,0.371300000000
420,0.134380000000,0.0040000000000,0.645600000000
425,0.214770000000,0.0073000000000,1.039050100000
430,0.283900000000,0.0116000000000,1.385600000000
435,0.328500000000,0.0168400000000,1.622960000000
440,0.348280000000,0.0230000000000,1.747060000000
445,0.348060000000,0.0298000000000,1.782600000000
450,0.336200000000,0.0380000000000,1.772110000000
455,0.318700000000,0.0480000000000,1.744100000000
460,0.290800000000,0.0600000000000,1.669200000000
465,0.251100000000,0.0739000000000,1.528100000000
470,0.195360000000,0.0909800000000,1.287640000000
475,0.142100000000,0.1126000000000,1.041900000000
480,0.095640000000,0.1390200000000,0.812950100000
485,0.057950010000,0.1693000000000,0.616200000000
490,0.032010000000,0.2080200000000,0.465180000000
495,0.014700000000,0.2586000000000,0.353300000000
500,0.004900000000,0.3230000000000,0.272000000000
505,0.002400000000,0.4073000000000,0.212300000000
510,0.009300000000,0.5030000000000,0.158200000000
515,0.029100000000,0.6082000000000,0.111700000000
520,0.063270000000,0.7100000000000,0.078249990000
525,0.109600000000,0.7932000000000,0.057250010000
530,0.165500000000,0.8620000000000,0.042160000000
535,0.225749900000,0.9148501000000,0.029840000000
540,0.290400000000,0.9540000000000,0.020300000000
545,0.359700000000,0.9803000000000,0.013400000000
550,0.433449900000,0.9949501000000,0.008749999000
555,0.512050100000,1.0000000000000,0.005749999000
560,0.594500000000,0.9950000000000,0.003900000000
565,0.678400000000,0.9786000000000,0.002749999000
570,0.762100000000,0.9520000000000,0.002100000000
575,0.842500000000,0.9154000000000,0.001800000000
580,0.916300000000,0.8700000000000,0.001650001000
585,0.978600000000,0.8163000000000,0.001400000000
590,1.026300000000,0.7570000000000,0.001100000000
595,1.056700000000,0.6949000000000,0.001000000000
600,1.062200000000,0.6310000000000,0.000800000000
605,1.045600000000,0.5668000000000,0.000600000000
610,1.002600000000,0.5030000000000,0.000340000000
615,0.938400000000,0.4412000000000,0.000240000000
620,0.854449900000,0.3810000000000,0.000190000000
625,0.751400000000,0.3210000000000,0.000100000000
630,0.642400000000,0.2650000000000,0.000049999990
635,0.541900000000,0.2170000000000,0.000030000000
640,0.447900000000,0.1750000000000,0.000020000000
645,0.360800000000,0.1382000000000,0.000010000000
650,0.283500000000,0.1070000000000,0.000000000000
655,0.218700000000,0.0816000000000,0.000000000000
660,0.164900000000,0.0610000000000,0.000000000000
665,0.121200000000,0.0445800000000,0.000000000000
670,0.087400000000,0.0320000000000,0.000000000000
675,0.063600000000,0.0232000000000,0.000000000000
680,0.046770000000,0.0170000000000,0.000000000000
685,0.032900000000,0.0119200000000,0.000000000000
690,0.022700000000,0.0082100000000,0.000000000000
695,0.015840000000,0.0057230000000,0.000000000000
700,0.011359160000,0.0041020000000,0.000000000000
705,0.008110916000,0.0029290000000,0.000000000000
710,0.005790346000,0.0020910000000,0.000000000000
715,0.004109457000,0.0014840000000,0.000000000000
720,0.002899327000,0.0010470000000,0.000000000000
725,0.002049190000,0.0007400000000,0.000000000000
730,0.001439971000,0.0005200000000,0.000000000000
735,0.000999949300,0.0003611000000,0.000000000000
740,0.000690078600,0.0002492000000,0.000000000000
745,0.000476021300,0.0001719000000,0.000000000000
750,0.000332301100,0.0001200000000,0.000000000000
755,0.000234826100,0.0000848000000,0.000000000000
760,0.000166150500,0.0000600000000,0.000000000000
765,0.000117413000,0.0000424000000,0.000000000000
770,0.000083075270,0.0000300000000,0.000000000000
775,0.000058706520,0.0000212000000,0.000000000000
780,0.000041509940,0.0000149900000,0.000000000000
785,0.000029353260,0.0000106000000,0.000000000000
790,0.000020673830,0.0000074657000,0.000000000000
795,0.000014559770,0.0000052578000,0.000000000000
800,0.000010253980,0.0000037029000,0.000000000000
805,0.000007221456,0.0000026078000,0.000000000000
810,0.000005085868,0.0000018366000,0.000000000000
815,0.000003581652,0.0000012934000,0.000000000000
820,0.000002522525,0.0000009109300,0.000000000000
825,0.000001776509,0.0000006415300,0.000000000000
830,0.000001251141,0.0000004518100,0.000000000000"""


def wavelength_grid(step_nm=10):
    """Supported study grids; the 5 nm grid is the refinement diagnostic."""
    if isinstance(step_nm, (bool, np.bool_)) or step_nm not in (5, 10):
        raise ValueError("IRCF wavelength step must be 5 or 10 nm")
    return np.arange(400.0, 1000.0 + float(step_nm), float(step_nm))


def _wave(value):
    wave = np.asarray(value, dtype=float)
    if (
        wave.ndim != 1
        or wave.size < 2
        or not np.isfinite(wave).all()
        or not np.all(np.diff(wave) > 0)
        or wave[0] != 400
        or wave[-1] != 1000
        or not np.any(wave == 700)
    ):
        raise ValueError("IRCF grid must increase from400 to1000 nm and include700 nm")
    return wave


def _widths(wave):
    widths = np.zeros_like(wave)
    widths[:-1] += np.diff(wave) / 2
    widths[1:] += np.diff(wave) / 2
    return widths


def _table(store, relative):
    path = store.resolve(relative)
    content = loadmat(path, simplify_cells=True)
    wave = np.asarray(content["wavelength"], float).reshape(-1)
    data = np.asarray(content["data"], float)
    if data.ndim == 1:
        data = data[:, None]
    if (
        wave.size < 2
        or data.ndim != 2
        or data.shape[0] != wave.size
        or not np.isfinite(wave).all()
        or not np.isfinite(data).all()
        or np.any(data < 0)
        or not np.all(np.diff(wave) > 0)
    ):
        raise ValueError(f"Invalid bounded spectral asset: {relative}")
    return (
        wave,
        data,
        {
            "file": relative,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "wavelength_range_nm": [float(wave[0]), float(wave[-1])],
            "native_sample_count": len(wave),
            "comment": str(content.get("comment", "")),
        },
    )


def _resample(wave, data, query):
    if np.any(query < wave[0]) or np.any(query > wave[-1]):
        raise ValueError("Requested spectral values exceed asset support; no extrapolation")
    return np.stack([np.interp(query, wave, column) for column in data.T], axis=-1)


def load_assets(workspace, wave=None):
    """Load pinned spectra, preserving densities and quadrature separately.

    ``xyz_per_photon`` includes trapezoidal wavelength weights and683 lm/W;
    multiply a photon-radiance *density* by it. ``photon_radiance_per_bin``
    already includes the weights for optics that accept per-band photons.
    Munsell has no NIR values; the returned training array ends at700 nm.
    """
    wave = _wave(wavelength_grid() if wave is None else wave)
    widths = _widths(wave)
    store = offline_asset_store(workspace=workspace, source=Path(workspace) / "camerae2e_db")
    tables = {key: _table(store, relative) for key, relative in _FILES.items()}
    cmf = np.asarray([[float(x) for x in row.split(",")] for row in _CMF_ROWS.splitlines()])
    cmf_values = np.stack(
        [np.interp(wave, cmf[:, 0], cmf[:, i], left=0, right=0) for i in (1, 2, 3)], axis=-1
    )
    energy_per_photon = h * c / (wave * 1e-9)
    xyz_per_photon_per_nm = 683 * cmf_values * energy_per_photon[:, None]
    xyz_per_photon = xyz_per_photon_per_nm * widths[:, None]
    illuminants = {}
    for key, table_key in (("day", "day"), ("ill_a", "ill_a"), ("d65_visible", "d65")):
        table_wave, data, _ = tables[table_key]
        energy = np.zeros_like(wave)
        active = wave <= 700 if key == "d65_visible" else np.ones_like(wave, dtype=bool)
        energy[active] = _resample(table_wave, data, wave[active])[:, 0]
        photons = energy / energy_per_photon
        unscaled_y = float(photons @ xyz_per_photon[:, 1])
        if unscaled_y <= 0:
            raise ValueError("Illuminant has no positive photopic luminance")
        scale = 1000 / unscaled_y
        photons *= scale
        illuminants[key] = {
            "photons_per_m2_sr_s_nm": photons,
            "photon_radiance_per_bin": photons * widths,
            "energy_w_per_m2_sr_nm": energy * scale,
            "white_xyz": photons @ xyz_per_photon / 1000,
            "luminance_cd_m2": float(photons @ xyz_per_photon[:, 1]),
            "source": table_key,
            "visible_only_calibration": key == "d65_visible",
        }
    macbeth = _resample(*tables["macbeth"][:2], wave)
    visible_wave = wave[wave <= 700]
    munsell = _resample(*tables["munsell"][:2], visible_wave)
    if macbeth.shape != (len(wave), 24) or munsell.shape != (len(visible_wave), 64):
        raise ValueError("Pinned Macbeth24/Munsell64 reflectance shape changed")
    if np.any(macbeth > 1) or np.any(munsell > 1):
        raise ValueError("Reflectance assets must lie in[0,1]")
    filters = {}
    for key in ("baseline_filter", "schott_filter"):
        native_wave, native_data, info = tables[key]
        if native_data.shape[1] != 1 or np.any(native_data > 1):
            raise ValueError("IRCF transmission must lie in[0,1]")
        filters[key] = {
            "wavelength_nm": native_wave,
            "transmission": native_data[:, 0],
            "metadata": info,
        }
    return {
        "schema": SCHEMA,
        "wavelength_nm": wave,
        "wavelength_bin_width_nm": widths,
        "illuminants": illuminants,
        "macbeth_reflectance": macbeth,
        "munsell_visible": {"wavelength_nm": visible_wave, "reflectance": munsell},
        "xyz_per_photon": xyz_per_photon,
        "xyz_per_photon_per_nm": xyz_per_photon_per_nm,
        "cmf_xyz": cmf_values,
        "filters": filters,
        "metadata": {
            "schema": SCHEMA,
            "assets": offline_asset_identity(workspace=workspace),
            "files": {key: item[2] for key, item in tables.items()},
            "wavelength_nm": wave.tolist(),
            "wavelength_bin_width_nm": widths.tolist(),
            "normalization": "1000 cd/m2 from fixed CIE1931 Y; no image normalization",
            "day_label": "local ill_DAY radiance spectrum, not CIE D65",
            "d65_fit_scope": "400..700 nm visible-only calibration; no invented Munsell NIR",
            "munsell_nir_available": False,
            "macbeth_used_in_fit": False,
            "cmf": {
                "source_url": CMF_URL,
                "source_sha256": CMF_SOURCE_SHA256,
                "doi": "10.25039/CIE.DS.xvudnb9b",
                "attribution": "CIE 2019, Colour-matching functions of CIE1931 standard observer",
                "license": "CC BY-SA 4.0",
                "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                "adaptation": "5 nm subset400..830 of original1 nm data; linear interpolation",
                "table_sha256": hashlib.sha256(_CMF_ROWS.encode()).hexdigest(),
                "outside_supported_830_nm": "zero",
            },
        },
    }


def filter_profiles():
    """Immutable-by-caller-copy controls; all variations are declared proxies."""
    return {
        key: {
            "id": key,
            "asset": "baseline_filter",
            "cutoff_shift_nm": shift,
            "nir_leak_floor": leak,
            "product_variant": False,
        }
        for key, shift, leak in (
            ("baseline", 0.0, 0.0),
            ("cutoff_minus20", -20.0, 0.0),
            ("cutoff_plus20", 20.0, 0.0),
            ("nir_leak_1pct", 0.0, 0.01),
        )
    }


def _smoothstep(value):
    value = np.clip(value, 0, 1)
    return value * value * (3 - 2 * value)


def filter_transmission(
    assets, profile, wavelength_nm, theta_deg=0.0, angular_mode="pupil", n_eff=1.8
):
    """Broadcast red-edge angular/shift proxy, not measured coating inference.

    ``normal`` and ``independent`` ignore angle. ``pupil`` uses each ray's AOI
    through sqrt(1-sin(theta)^2/n_eff^2). Only the red edge is changed: the
    transformation blends in from600 to620 nm, preserving the measured UV edge.
    """
    if isinstance(profile, str):
        try:
            profile = filter_profiles()[profile]
        except KeyError as exc:
            raise ValueError("Unknown IRCF profile") from exc
    if not isinstance(profile, dict) or profile.get("asset") not in assets["filters"]:
        raise ValueError("IRCF profile must reference a loaded filter")
    if angular_mode not in ("normal", "independent", "pupil"):
        raise ValueError("IRCF angular mode must be normal, independent or pupil")
    if isinstance(n_eff, bool) or not np.isfinite(n_eff) or n_eff <= 1:
        raise ValueError("IRCF effective index must be finite and greater than1")
    wave, theta = np.broadcast_arrays(
        np.asarray(wavelength_nm, float), np.asarray(theta_deg, float)
    )
    if (
        not np.isfinite(wave).all()
        or not np.isfinite(theta).all()
        or np.any((wave < 400) | (wave > 1000))
        or np.any((theta < 0) | (theta > 60))
    ):
        raise ValueError("IRCF queries require400..1000 nm and AOI0..60 degrees")
    shift, leak = float(profile.get("cutoff_shift_nm", 0)), float(profile.get("nir_leak_floor", 0))
    if (
        not np.isfinite(shift)
        or not -40 <= shift <= 40
        or not np.isfinite(leak)
        or not 0 <= leak <= 1
    ):
        raise ValueError("IRCF shift must be within40 nm and leak floor within[0,1]")
    factor = np.sqrt(1 - (np.sin(np.deg2rad(theta)) / n_eff) ** 2)
    if angular_mode != "pupil":
        factor = np.ones_like(factor)
    query = wave + _smoothstep((wave - 600) / 20) * (wave / factor - shift - wave)
    table = assets["filters"][profile["asset"]]
    native_wave = np.asarray(table["wavelength_nm"], float)
    if np.any(query < native_wave[0]) or np.any(query > native_wave[-1]):
        raise ValueError("Angular IRCF proxy exceeds source spectrum; no extrapolation")
    result = np.interp(query, native_wave, table["transmission"])
    return np.maximum(result, leak * _smoothstep((wave - 700) / 50))


def build_pixel_response(workspace, wave=None, nir_scale=1.0):
    """Return a visible physical + separately labeled NIR QE response.

    NIR normal EQE anchors are shared by all sites at800/900/1000 nm. Each
    channel joins its actual700 nm visible EQE continuously. The700 nm angular
    ratio is frozen into the NIR; this is an assumption, not material dispersion.
    """
    from pyisetcam.physical_estimate import physical_optical_response

    wave = _wave(wavelength_grid() if wave is None else wave)
    if (
        isinstance(nir_scale, (bool, np.bool_))
        or not np.isfinite(nir_scale)
        or not 0 < nir_scale <= 2
    ):
        raise ValueError("NIR QE sensitivity scale must be finite in(0,2]")
    cell = reference_pixel.build_cell(workspace)
    theta = np.arange(0.0, 61.0, 5.0)
    visible = wave <= 700
    budget = physical_optical_response(
        cell["physical_model"], wave[None, visible], theta_deg=theta[:, None], phi_deg=0
    )
    qe = np.zeros((len(theta), len(wave), 4))
    qe[:, visible] = budget["eqe"]
    anchor700 = qe[:, np.flatnonzero(wave == 700)[0]]
    ratios = np.divide(
        anchor700, anchor700[0], out=np.zeros_like(anchor700), where=anchor700[0] > 0
    )
    nir_normal = np.stack(
        [
            np.interp(
                wave[~visible],
                [700, 800, 900, 1000],
                [anchor700[0, site], 0.20 * nir_scale, 0.12 * nir_scale, 0.02 * nir_scale],
            )
            for site in range(4)
        ],
        axis=-1,
    )
    qe[:, ~visible] = ratios[:, None, :] * nir_normal[None]
    if not np.isfinite(qe).all() or np.any((qe < 0) | (qe > 1)):
        raise ValueError("Hybrid response exceeds physical EQE bounds; review NIR assumptions")
    metadata = {
        "schema": PIXEL_SCHEMA,
        "visible_range_nm": [400, 700],
        "visible_model": "existing physical_optical_response; unmodified material support",
        "nir_range_nm": [700, 1000],
        "nir_model": "declared absolute-QE sensitivity proxy, not IMX462 product QE",
        "nir_anchor_wavelength_nm": [700, 800, 900, 1000],
        "nir_anchor_normal_qe": [
            anchor700[0].tolist(),
            [0.20 * nir_scale] * 4,
            [0.12 * nir_scale] * 4,
            [0.02 * nir_scale] * 4,
        ],
        "nir_scale": float(nir_scale),
        "nir_angular_model": "frozen channelwise700 nm absolute/normal QE ratio",
        "nir_material_extrapolation": False,
        "product_measured": False,
        "source_cell_sha256": cell["cell_sha256"],
        "source_material_bundle_sha256": cell["provenance"]["source_material_bundle_sha256"],
        "source_optical_model_sha256": cell["provenance"]["source_optical_model_sha256"],
        "source_evidence_sha256": cell["provenance"]["source_evidence_sha256"],
        "qe_contract": "absolute collected electrons per sensor-plane incident photon",
        "interpolation": "linear wavelength and theta; no extrapolation",
        "sites": ["R", "Gr", "Gb", "B"],
    }
    response = {
        "schema": PIXEL_SCHEMA,
        "cell_sha256": cell["cell_sha256"],
        "wavelength_nm": wave.tolist(),
        "theta_deg": theta.tolist(),
        "sites": ["R", "Gr", "Gb", "B"],
        "array_axes": ["theta_deg", "wavelength_nm", "site"],
        "eqe": qe.tolist(),
        "normal_qe": qe[0].tolist(),
        "provenance": copy.deepcopy(metadata),
    }
    response["table_sha256"] = digest(response)
    return {"cell": cell, "response": response, "metadata": metadata}


def absolute_response(response, theta_deg, phi_deg, wavelength_nm):
    """Optics callback, broadcasting(theta,phi,wavelength) and appending4 sites."""
    if response.get("schema") != PIXEL_SCHEMA:
        raise ValueError("Expected an explicit hybrid IRCF pixel response")
    theta, phi, wave = np.broadcast_arrays(theta_deg, phi_deg, wavelength_nm)
    if not all(np.isfinite(value).all() for value in (theta, phi, wave)):
        raise ValueError("IRCF pixel query must be finite")
    angles = np.asarray(response["theta_deg"], float)
    wavelengths = np.asarray(response["wavelength_nm"], float)
    if (
        np.any(theta < angles[0])
        or np.any(theta > angles[-1])
        or np.any(wave < wavelengths[0])
        or np.any(wave > wavelengths[-1])
    ):
        raise ValueError("IRCF pixel query exceeds declared support; no extrapolation")
    values = np.asarray(response["eqe"], float)
    if (
        values.shape != (len(angles), len(wavelengths), 4)
        or not np.isfinite(values).all()
        or np.any((values < 0) | (values > 1))
    ):
        raise ValueError("Invalid hybrid response table")
    ti = np.clip(np.searchsorted(angles, theta, side="right"), 1, len(angles) - 1)
    wi = np.clip(np.searchsorted(wavelengths, wave, side="right"), 1, len(wavelengths) - 1)
    ft = ((theta - angles[ti - 1]) / (angles[ti] - angles[ti - 1]))[..., None]
    fw = ((wave - wavelengths[wi - 1]) / (wavelengths[wi] - wavelengths[wi - 1]))[..., None]
    return (1 - ft) * ((1 - fw) * values[ti - 1, wi - 1] + fw * values[ti - 1, wi]) + ft * (
        (1 - fw) * values[ti, wi - 1] + fw * values[ti, wi]
    )
