"""Tiled analytic wide-angle capture using saved sensor QE and RAW readout.

This is a declared parametric camera, never a rewritten patent prescription.
Spatially varying blur uses fixed global field nodes, independent of tiles.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import resource
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from scipy.interpolate import RegularGridInterpolator
from scipy.signal import fftconvolve
from scipy.special import j1

from .road_rgb_source import file_sha256
from .virtual_lab_wide_geometry import (
    angular_chart_rois,
    chart_pattern,
    pixel_directions,
    resolution_sites,
)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False))
    os.replace(temporary, path)


def preflight(root, geometry, controls, chart_count=1, noise_repeats=0):
    wide = geometry["wide_angle"]
    rows, cols = geometry["native_shape"]
    factor = 8 if wide["sampling"] == "auto" else int(wide["sampling"])
    pitch = geometry["pixel_pitch_um"]
    blur = max(wide["radial_blur_um"], wide["tangential_blur_um"])
    support_um = (
        10 * 0.650 * controls["f_number"]
        + abs(controls["defocus_um"]) / (2 * controls["f_number"])
        + 5 * blur
    )
    halo = int(math.ceil(support_um / pitch * factor)) + 2 * factor
    source_side = wide["tile_size"] * factor + 2 * halo
    # Simultaneous FFT, spectra, direction/gain and density planes plus native
    # ISP working arrays. This is admission arithmetic, not an OS RSS limit.
    working = int(source_side**2 * 240 + rows * cols * 192)
    output = int(rows * cols * (chart_count * 104 + noise_repeats * 26) + 64 * 1024**2)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    if working > wide["max_working_bytes"]:
        raise ValueError(
            f"Wide resource policy: estimated {working:,} working bytes exceeds "
            f"{wide['max_working_bytes']:,}; change explicit tile policy, not sampling silently"
        )
    if free - output < wide["min_free_disk_bytes"]:
        raise ValueError(
            f"Wide disk policy: estimated {output:,} bytes leaves less than "
            "the declared free-space reserve"
        )
    return {
        "estimated_working_bytes": working,
        "estimated_output_bytes": output,
        "observed_free_bytes": free,
        "policy_is_not_hardware_limit": True,
        "hard_rss_limit_enforced": False,
        "native_shape": [rows, cols],
        "highest_checked_sampling_factor": factor,
        "optical_support_radius_um": support_um,
    }


def build_angular_table(geometry, controls, angular, physical_table, wave_nm):
    """Declared exit-pupil disk,64 equal-area samples; no patent-ray identity."""
    from .virtual_lab_angular import interpolate_relative

    wide = geometry["wide_angle"]
    rows, cols = geometry["native_shape"]
    # Cover the native field and the largest declared optical halo.
    maximum = math.hypot(rows, cols) * geometry["pixel_pitch_um"] * 0.001 / 2 + 0.5
    radius = np.linspace(0, maximum, 49)
    azimuth = np.arange(16) * 22.5
    exit_distance = wide["exit_pupil_distance_mm"]
    pupil_radius = exit_distance / (2 * controls["f_number"])
    rho = np.repeat(np.sqrt((np.arange(4) + 0.5) / 4), 16) * pupil_radius
    phi = np.tile(np.arange(16) * 2 * np.pi / 16, 4)
    pupil_x, pupil_y = rho * np.cos(phi), rho * np.sin(phi)
    values = np.ones((len(wave_nm), len(radius), len(azimuth), 4))
    dx = radius[:, None] - pupil_x[None, :]
    dy = np.broadcast_to(-pupil_y, dx.shape)
    if angular["mode"] != "physical_pupil":
        dx = radius[:, None]
        dy = np.zeros_like(dx)
    theta = np.degrees(np.arctan2(np.hypot(dx, dy), exit_distance))
    sensor_phi = (np.degrees(np.arctan2(dy, dx))[..., None] + azimuth) % 360
    theta_min, theta_max = float(theta.min()), float(theta.max())
    for wi, wavelength in enumerate(wave_nm):
        if angular["mode"] == "off":
            continue
        if angular["mode"] == "cosine_power":
            gain = np.cos(np.radians(theta))[..., None, None] ** np.asarray(
                angular["exponents_r_gr_gb_b"]
            )
            values[wi] = np.broadcast_to(gain.mean(axis=1), (len(radius), len(azimuth), 4))
        else:
            gain = interpolate_relative(
                physical_table, theta[..., None], sensor_phi, float(wavelength)
            )
            values[wi] = gain.mean(axis=1)
    table = {
        "schema": "camera_virtual_lab_parametric_exit_pupil_v1",
        "wavelength_nm": list(map(float, wave_nm)),
        "radius_mm": radius.tolist(),
        "azimuth_deg": azimuth.tolist(),
        "relative_response": values.tolist(),
        "provenance": {
            "kind": "declared_parametric_exit_pupil_disk",
            "exit_pupil_distance_mm": exit_distance,
            "pupil_radius_mm": pupil_radius,
            "image_space_f_number": controls["f_number"],
            "pupil_sample_count": 64 if angular["mode"] == "physical_pupil" else 1,
            "quadrature": "4 equal-area radial midpoints ×16 azimuth samples",
            "direction_theta_deg_min": theta_min,
            "direction_theta_deg_max": theta_max,
            "normal_qe_applied_once": True,
            "absolute_throughput_claim": False,
            "joint_psf_angle_transport": False,
            "physical_relative_table_sha256": None
            if physical_table is None
            else physical_table.get("table_sha256"),
        },
    }
    table["table_sha256"] = _digest(table)
    return table


class TileOptics:
    def __init__(self, optics, sensor, spectra, geometry, controls, context):
        from pyisetcam.sensor import _sensor_qe_on_wave

        from .virtual_lab_engine import WAVE

        self.wave = WAVE
        self.optics = optics
        self.sensor = sensor
        self.spectra = spectra
        self.geo = geometry
        self.controls = controls
        self.context = context
        self.wide = geometry["wide_angle"]
        self.sites = resolution_sites(geometry)
        self.qe = _sensor_qe_on_wave(sensor, WAVE)
        self.kernels = {}
        self.interpolators = []
        table = context["wide_angular_table"]
        angles = np.asarray(table["azimuth_deg"])
        for values in np.asarray(table["relative_response"]):
            self.interpolators.append(
                RegularGridInterpolator(
                    (table["radius_mm"], np.r_[angles, 360]),
                    np.concatenate([values, values[:, :1]], axis=1),
                    bounds_error=True,
                )
            )
        pixel = sensor.fields["pixel"]
        if not np.allclose(pixel["pd_size_m"], pixel["size_m"]) or not math.isclose(
            pixel["fill_factor"], 1
        ):
            raise ValueError(
                "Wide pixel area integration currently requires the saved full-pixel PD aperture"
            )

    def kernel(self, band, factor, node):
        key = (band, factor, node)
        if key in self.kernels:
            return self.kernels[key]
        pitch = self.geo["pixel_pitch_um"] / factor
        wave = self.wave[band] * 0.001
        fn = self.controls["f_number"]
        radial, tangential = self.wide["radial_blur_um"], self.wide["tangential_blur_um"]
        ny, nx = node
        rows, cols = self.geo["native_shape"]
        physical_x = (nx - 2) * cols / 4
        physical_y = (ny - 2) * rows / 4
        rr = math.hypot(physical_x, physical_y) / (math.hypot(rows, cols) / 2)
        angle = math.atan2(physical_y, physical_x)
        sr, st = radial * rr * rr, tangential * rr * rr
        radius = 10 * wave * fn + abs(self.controls["defocus_um"]) / (2 * fn) + 5 * max(sr, st)
        half = int(math.ceil(radius / pitch))
        y, x = np.meshgrid(
            np.arange(-half, half + 1) * pitch, np.arange(-half, half + 1) * pitch, indexing="ij"
        )
        z = np.pi * np.hypot(x, y) / (wave * fn)
        k = np.divide(2 * j1(z), z, out=np.ones_like(z), where=z != 0) ** 2
        defocus = abs(self.controls["defocus_um"]) / (2 * fn)
        if defocus > pitch / 4:
            disk = (np.hypot(x, y) <= defocus).astype(float)
            k = fftconvolve(k, disk / disk.sum(), mode="same")
        if max(sr, st) > 0:
            # A discrete covariance Gaussian is sampled on this same grid.
            xr = x * math.cos(angle) + y * math.sin(angle)
            yt = -x * math.sin(angle) + y * math.cos(angle)
            sr = max(sr, pitch * 0.18)
            st = max(st, pitch * 0.18)
            g = np.exp(-0.5 * ((xr / sr) ** 2 + (yt / st) ** 2))
            k = fftconvolve(k, g / g.sum(), mode="same")
        k = np.maximum(k, 0)
        k = k / k.sum()
        self.kernels[key] = k
        return k

    def evaluate(self, kind, bounds, factor):
        r0, r1, c0, c1 = map(int, bounds)
        nr, nc = r1 - r0, c1 - c0
        rows, cols = self.geo["native_shape"]
        pitch = self.geo["pixel_pitch_um"]
        varying = max(self.wide["radial_blur_um"], self.wide["tangential_blur_um"]) > 0
        # Fixed5×5 receiver nodes make the operator independent of execution tiles.
        rr = (np.arange(r0 * factor, r1 * factor) + 0.5) / factor - 0.5
        cc = (np.arange(c0 * factor, c1 * factor) + 0.5) / factor - 0.5
        y, x = np.meshgrid(rr, cc, indexing="ij")
        node_weights = {}
        if varying:
            fy = (y + 0.5) / rows * 4
            fx = (x + 0.5) / cols * 4
            for iy in range(5):
                for ix in range(5):
                    weight = np.maximum(1 - abs(fy - iy), 0) * np.maximum(1 - abs(fx - ix), 0)
                    if weight.max() > 0:
                        node_weights[iy, ix] = weight
        else:
            node_weights[2, 2] = 1.0
        halo = (
            max(
                max(self.kernel(b, factor, n).shape) // 2
                for b in range(len(self.wave))
                for n in node_weights
            )
            + factor
        )
        sy = (np.arange(r0 * factor - halo, r1 * factor + halo) + 0.5) / factor - 0.5
        sx = (np.arange(c0 * factor - halo, c1 * factor + halo) + 0.5) / factor - 0.5
        source_y, source_x = np.meshgrid(sy, sx, indexing="ij")
        if isinstance(kind, dict):
            # Continuous native-frequency diagnostic in the local ray mapping;
            # evaluated before optical convolution and receiving pixel area.
            pattern = 0.5 * (
                1
                + 0.8
                * np.cos(
                    2 * np.pi * kind["frequency"] * (source_x - kind["center_col"]) + kind["phase"]
                )
            )
        else:
            pattern = chart_pattern(kind, source_y, source_x, self.geo, self.sites)
        directions = pixel_directions(source_y, source_x, self.geo)
        illumination = np.maximum(directions[..., 2], 0) ** self.wide["relative_illumination_power"]
        optical_scale = (
            math.pi / (1 + 4 * self.controls["f_number"] ** 2) * self.wide["transmission"]
        )
        receiver_x = (x + 0.5 - cols / 2) * pitch * 0.001
        receiver_y = (y + 0.5 - rows / 2) * pitch * 0.001
        lookup = np.stack(
            [
                np.hypot(receiver_x, receiver_y),
                np.mod(np.degrees(np.arctan2(receiver_y, receiver_x)), 360),
            ],
            axis=-1,
        )
        density = np.zeros((*x.shape, 4))
        baseline = np.zeros((*x.shape, 3))
        preview = np.zeros(x.shape)
        for band in range(len(self.wave)):
            radiance = (
                self.spectra["patches"][pattern, band]
                if kind == "color"
                else pattern * self.spectra["white"][band]
            )
            radiance = radiance * illumination * optical_scale
            plane = np.zeros(x.shape)
            for node, weight in node_weights.items():
                full = fftconvolve(radiance, self.kernel(band, factor, node), mode="same")
                plane += full[halo : halo + nr * factor, halo : halo + nc * factor] * weight
            gain = self.interpolators[band](lookup)
            for channel in range(3):
                baseline[..., channel] += plane * self.qe[band, channel] * 10
            for site, channel in enumerate([0, 1, 1, 2]):
                density[..., site] += plane * self.qe[band, channel] * 10 * gain[..., site]
            preview += plane / len(self.wave)
        exposure = self.sensor.fields["integration_time"]
        area = (pitch * 1e-6) ** 2
        integrated = density.reshape(nr, factor, nc, factor, 4).mean(axis=(1, 3)) * area * exposure
        unmodified = baseline.reshape(nr, factor, nc, factor, 3).mean(axis=(1, 3)) * area * exposure
        rr, cc = np.meshgrid(np.arange(r0, r1), np.arange(c0, c1), indexing="ij")
        site = (rr % 2) * 2 + cc % 2
        channel = np.array([0, 1, 1, 2])[site]
        mean = np.take_along_axis(integrated, site[..., None], axis=-1)[..., 0]
        before = np.take_along_axis(unmodified, channel[..., None], axis=-1)[..., 0]
        px = (cc + 0.5 - cols / 2) * pitch * 0.001
        py = (rr + 0.5 - rows / 2) * pitch * 0.001
        rad = np.hypot(px, py)
        theta = np.degrees(np.arctan2(rad, self.wide["exit_pupil_distance_mm"]))
        gain = self.interpolators[len(self.wave) // 2](
            np.stack([rad, np.mod(np.degrees(np.arctan2(py, px)), 360)], axis=-1)
        )
        result = {
            "mean": np.maximum(mean, 0),
            "mean_without_angular": np.maximum(before, 0),
            "optical": preview.reshape(nr, factor, nc, factor).mean(axis=(1, 3)),
            "cra_deg": theta,
            "gain_r_gr_gb_b": gain,
        }
        if isinstance(kind, dict):
            result["probe_signal"] = integrated.sum(axis=-1)
        return result


def choose_sampling(evaluator, configured, checkpoint):
    """Nine representative native ROIs; global factor, explicit unresolved status."""
    if configured != "auto":
        return {
            "status": "explicit_unverified",
            "factor": int(configured),
            "selected_factor": int(configured),
            "reason": "User-selected grid; no automatic refinement claim",
        }
    records = []
    previous = None
    chosen = 8
    verified = False
    for factor in (2, 4, 8):
        values = []
        for site_index, site in enumerate(evaluator.sites.values()):
            edge = site["radial"]
            r0, r1, c0, c1 = edge["bounds_row_col"]
            r = (r0 + r1) // 2
            c = (c0 + c1) // 2
            result = evaluator.evaluate("resolution", [r - 10, r + 10, c - 10, c + 10], factor)[
                "mean"
            ]
            # Green sites avoid interpreting CFA color variation as contrast.
            green = result[::2, 1::2]
            lo, hi = np.percentile(green, [10, 90])
            frequency_contrasts = []
            for frequency in (0.25, 0.5):
                for phase in (0.0, math.pi / 4):
                    signal = evaluator.evaluate(
                        {"frequency": frequency, "phase": phase, "center_col": c},
                        [r - 8, r + 8, c - 8, c + 8],
                        factor,
                    )["probe_signal"]
                    basis = np.cos(2 * np.pi * frequency * (np.arange(c - 8, c + 8) - c) + phase)
                    amplitude = np.mean(signal, axis=0) @ basis / (basis @ basis)
                    frequency_contrasts.append(float(abs(amplitude) / max(signal.mean(), 1e-30)))
            values.append(
                {
                    "mean": float(green.mean()),
                    "contrast": float((hi - lo) / max(hi + lo, 1e-30)),
                    "frequency_contrasts": frequency_contrasts,
                }
            )
            if evaluator.context.get("on_band"):
                evaluator.context["on_band"](site_index + 1, 9)
        step = {"factor": factor, "sites": values}
        if previous is not None:
            mean_change = max(
                abs(a["mean"] - b["mean"]) / max(abs(a["mean"]), 1e-30)
                for a, b in zip(values, previous, strict=True)
            )
            contrast_change = max(
                abs(a["contrast"] - b["contrast"]) for a, b in zip(values, previous, strict=True)
            )
            frequency_change = max(
                float(
                    np.max(
                        abs(
                            np.asarray(a["frequency_contrasts"])
                            - np.asarray(b["frequency_contrasts"])
                        )
                    )
                )
                for a, b in zip(values, previous, strict=True)
            )
            step.update(
                max_relative_mean_change=mean_change,
                max_contrast_change=contrast_change,
                max_frequency_contrast_change=frequency_change,
            )
            if mean_change <= 0.01 and contrast_change <= 0.02 and frequency_change <= 0.02:
                chosen = factor // 2
                verified = True
                records.append(step)
                break
        records.append(step)
        previous = values
    result = {
        "status": "representative_refinement_passed"
        if verified
        else "unverified_at_sampling_limit",
        "factor": chosen,
        "selected_factor": chosen,
        "mean_tolerance": 0.01,
        "contrast_tolerance": 0.02,
        "representative_sites": 9,
        "frequency_probes_cycles_per_native_pixel": [0.25, 0.5],
        "frequency_probe_phases_rad": [0.0, math.pi / 4],
        "verified_against_factor": factor if verified else None,
        "steps": records,
        "scope": "nine native edge probes plus0.25/0.5cy/nativepixel sinusoidal contrast "
        "at2phases; not whole-scene convergence certification",
    }
    _json(checkpoint, result)
    return result


def coordinate_readout(sensor, mean, output_dir, seed, wide):
    from .lens_sensor_out_of_core_readout import (
        OutOfCoreReadoutPolicy,
        _config,
        compute_out_of_core_readout,
    )

    rows, cols = mean.shape
    output_dir = Path(output_dir)
    policy = OutOfCoreReadoutPolicy(
        rows * cols,
        cols * 64,
        wide["max_working_bytes"],
        rows * cols * 24 + 65536,
        wide["max_working_bytes"],
        wide["min_free_disk_bytes"],
    )
    if output_dir.exists():
        metadata = json.loads((output_dir / "metadata.json").read_text())
        current = _config(sensor)
        previous = metadata["sensor_capture"]
        if (
            previous["readout"] != current["readout"]
            or previous["exposure_time_s"] != current["exposure_time_s"]
            or previous["fixed_pattern_seed"] != current["fixed_pattern_seed"]
            or previous["noise_flag"] != current["noise_flag"]
        ):
            raise ValueError("Wide readout checkpoint configuration changed")
        expected = metadata["output_files"]["raw_dn"]["sha256"]
        if file_sha256(output_dir / "raw_dn.npy") != expected:
            raise ValueError("Completed wide readout RAW hash changed")
        digest = hashlib.sha256()
        for r in range(0, rows, 64):
            digest.update(np.ascontiguousarray(mean[r : r + 64]).tobytes())
        if (
            digest.hexdigest() != metadata["mean_input_content_sha256"]
            or metadata["sensor_capture"]["noise_seed"] != seed
        ):
            raise ValueError("Wide readout checkpoint identity differs")
    else:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        metadata = compute_out_of_core_readout(
            sensor, mean, output_dir, seed=int(seed), row_chunk_rows=64, policy=policy
        )
    result = sensor.clone()
    result.data = {
        "mean_electrons": mean,
        "raw_dn": np.load(output_dir / "raw_dn.npy", mmap_mode="r"),
    }
    result.metadata["sensor_capture"] = metadata["sensor_capture"]
    return result


def capture(optics, sensor, spectra, kind, geometry, controls, seed, context):
    from .virtual_lab_field import _profile

    started = time.monotonic()
    root = Path(context["output_dir"])
    wide = geometry["wide_angle"]
    checkpoint = root / "wide-checkpoints" / kind
    checkpoint.mkdir(parents=True, exist_ok=True)
    evaluator = TileOptics(optics, sensor, spectra, geometry, controls, context)
    if "wide_sampling" not in context:
        context["wide_sampling"] = choose_sampling(
            evaluator, wide["sampling"], root / "wide-checkpoints/sampling.json"
        )
    sampling = context["wide_sampling"]
    factor = sampling["factor"]
    identity = _digest(
        {
            "geometry": geometry,
            "controls": controls,
            "spectrum": np.asarray(spectra["white"]).tolist(),
            "kind": kind,
            "seed": seed,
            "factor": factor,
            "pupil": context["wide_angular_table"]["table_sha256"],
            "readout": sensor.fields["sensor_readout"],
            "integration_time_s": float(sensor.fields["integration_time"]),
            "qe_sha256": hashlib.sha256(
                np.asarray(evaluator.qe, dtype="<f8").tobytes()
            ).hexdigest(),
            "patch_spectra_sha256": hashlib.sha256(
                np.asarray(spectra["patches"], dtype="<f8").tobytes()
            ).hexdigest(),
            "pd_size_m": np.asarray(sensor.fields["pixel"]["pd_size_m"]).tolist(),
            "pd_position_m": np.asarray(sensor.fields["pixel"]["pd_position_m"]).tolist(),
            "fill_factor": float(sensor.fields["pixel"]["fill_factor"]),
            "acquisition_spec": {
                k: v
                for k, v in context.get("specification", {}).items()
                if k not in {"name", "isp"}
            },
        }
    )
    manifest_path = checkpoint / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {"schema": "camera_virtual_lab_wide_tiles_v1", "identity": identity, "tiles": {}}
    )
    if manifest["identity"] != identity:
        raise ValueError("Wide acquisition checkpoint identity mismatch")
    rows, cols = geometry["native_shape"]
    tile = wide["tile_size"]
    arrays = {
        key: np.lib.format.open_memmap(
            checkpoint / f"{key}.npy",
            mode="w+",
            dtype=np.float32,
            shape=(rows, cols, 4) if key == "gain_r_gr_gb_b" else (rows, cols),
        )
        for key in ("mean", "mean_without_angular", "optical", "cra_deg", "gain_r_gr_gb_b")
    }
    count = math.ceil(rows / tile) * math.ceil(cols / tile)
    done = 0
    reused = 0
    for r0 in range(0, rows, tile):
        for c0 in range(0, cols, tile):
            bounds = [r0, min(rows, r0 + tile), c0, min(cols, c0 + tile)]
            name = f"tile-{r0}-{c0}.npz"
            path = checkpoint / name
            entry = manifest["tiles"].get(name)
            if entry:
                if not path.is_file() or file_sha256(path) != entry["sha256"]:
                    raise ValueError("Completed wide tile hash differs; resume refused")
                with np.load(path) as loaded:
                    data = {k: loaded[k] for k in arrays}
                reused += 1
            else:
                data = evaluator.evaluate(kind, bounds, factor)
                temporary = path.with_suffix(".tmp.npz")
                np.savez(temporary, **{k: np.asarray(v, np.float32) for k, v in data.items()})
                os.replace(temporary, path)
                manifest["tiles"][name] = {"sha256": file_sha256(path), "bounds": bounds}
                _json(manifest_path, manifest)
            r0, r1, c0, c1 = bounds
            for key, array in arrays.items():
                array[r0:r1, c0:c1] = data[key]
            done += 1
            if context.get("on_band"):
                context["on_band"](done, count)
    for array in arrays.values():
        array.flush()
    captured = coordinate_readout(sensor, arrays["mean"], checkpoint / "readout", seed, wide)
    theta, gain = arrays["cra_deg"], arrays["gain_r_gr_gb_b"]
    summary = {
        "status": "ok",
        "mode": "full_field",
        "angular_mode": context["angular_response"]["mode"],
        "chief_ray_only": context["angular_response"]["mode"] != "physical_pupil",
        "pupil_averaged": context["angular_response"]["mode"] == "physical_pupil",
        "not_pupil_averaged": context["angular_response"]["mode"] != "physical_pupil",
        "cra_origin": "declared_parametric_exit_pupil",
        "reference_wavelength_nm": 550.0,
        "native_shape": [rows, cols],
        "cra_deg": {
            "min": float(theta.min()),
            "max": float(theta.max()),
            "mean": float(theta.mean()),
        },
        "gain_r_gr_gb_b": {
            "min": gain.min(axis=(0, 1)).tolist(),
            "max": gain.max(axis=(0, 1)).tolist(),
            "mean": gain.mean(axis=(0, 1)).tolist(),
        },
        "before_after_mean_electrons": {
            "without_angular": float(arrays["mean_without_angular"].mean()),
            "with_angular": float(arrays["mean"].mean()),
            "ratio": float(arrays["mean"].mean() / arrays["mean_without_angular"].mean()),
        },
        "profile": _profile(theta, gain),
        "angular_provenance": context["wide_angular_table"]["provenance"],
        "gain_map_display_range": [0, 1.5],
        "cra_map_display_range_deg": [0, 60],
        "angular_coupling_scope": "parametric pupil response, receiver-field separable; "
        "no joint PSF/angle transport",
    }
    captured.metadata["virtual_lab_field"] = {
        "summary": summary,
        "cra_deg": theta,
        "gain_r_gr_gb_b": gain,
        "mean_without_angular": arrays["mean_without_angular"],
    }
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
        1 if sys.platform == "darwin" else 1024
    )
    captured.metadata["virtual_lab_wide"] = {
        "projection": geometry["projection"],
        "resolution_sites": evaluator.sites,
        "color_rois_native": angular_chart_rois(geometry, "color"),
        "tone_rois_native": angular_chart_rois(geometry, "tone"),
        "sampling": sampling,
        "resource": {
            **context["wide_preflight"],
            "capture_elapsed_s": time.monotonic() - started,
            "process_peak_rss_bytes": peak,
            "rss_is_process_lifetime_peak": True,
        },
        "checkpoint": {
            "directory": str(checkpoint.relative_to(root)),
            "tiles": count,
            "reused_tiles": reused,
            "identity": identity,
        },
        "coordinate_readout": captured.metadata["sensor_capture"],
        "pixel_integration": "area average over saved full-pixel PD aperture "
        "before CFA mosaic and readout",
        "optical_blur_model": "normalized Airy/defocus plus declared radial/tangential "
        "Gaussian; fixed5×5 receiver-field interpolation",
        "scene_scope": "shared direction-defined scene and9-site target; "
        "each projection maps the same angular features into native coordinates",
    }
    return captured, arrays["optical"], evaluator.kernel(len(evaluator.wave) // 2, factor, (2, 2))
