"""Output-coordinate space-variant optical PSFs with bounded overlap-save tiles.

Nine directly traced, RGB-normalized optical kernels form a nonnegative triangle
basis. Weights are evaluated at receiving native-pixel centers and are constant
within that pixel's area integral. This preserves DC, not global point-source
flux; absolute radiometry/distortion transport is deliberately outside the model.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

import numpy as np
import scipy
from scipy.fft import irfft2, next_fast_len, rfft2

from . import calibration_db_core_engine as core
from . import calibration_direct_convergence as direct
from . import calibration_direct_optics as optics
from . import reference_isp
from .registry import digest

POLICY = "camera_spatial_direct_psf_overlap_save_v1"
TILE = 128
CACHE_BYTES = 96 * 1024**2
MAX_ARRAY_BYTES = 640 * 1024**2
TRIANGLES = [
    ["crop-nw", "crop-n", "crop-center"],
    ["crop-nw", "crop-center", "crop-w"],
    ["crop-n", "crop-ne", "crop-e"],
    ["crop-n", "crop-e", "crop-center"],
    ["crop-w", "crop-center", "crop-s"],
    ["crop-w", "crop-s", "crop-sw"],
    ["crop-center", "crop-e", "crop-se"],
    ["crop-center", "crop-se", "crop-s"],
]
LIMITS = {
    "tile_raw_max_dn": 0,
    "tile_isp_max_abs": 1e-12,
    "window_raw_max_dn": 0,
    "window_isp_max_abs": 1e-12,
    "stationary_mtf_max_abs": 0.00001,
    "stationary_raw_max_dn": 1,
    "stationary_isp_max_abs": 0.0001,
    "stationary_mtf50_relative": 0.0001,
}
SCOPE = [
    (
        "Nine directly traced D65/QE-weighte"
        "d PSFs; triangle interpolation only"
        " between those anchors."
    ),
    (
        "Output-coordinate normalized optica"
        "l operator; native-center weights h"
        "eld through one receiving-pixel are"
        "a integral."
    ),
    (
        "Outside the 9-anchor convex rectang"
        "le: clamp to its nearest boundary. "
        "No untraced corner optics are inven"
        "ted."
    ),
    (
        "Preserves spatially constant respon"
        "se; does not qualify point-source t"
        "otal flux, absolute field irradianc"
        "e or vignetting."
    ),
    (
        "Continuous sensor-plane chart or ex"
        "plicitly sampled native-raster reco"
        "nstruction; no scene-to-sensor dist"
        "ortion transport."
    ),
    (
        "No global supersampled image or per"
        "-pixel kernel map; sequential chann"
        "el/tile processing with a bounded F"
        "FT cache."
    ),
    (
        "D65 32000 photons/pixel; noiseless "
        "shared calibrated electronics, QE a"
        "nd CCM; zero unidentified charge di"
        "ffusion."
    ),
    (
        "Spatial variation inside a Crop is "
        "a model change: local stationary-Cr"
        "op residuals are reported, not requ"
        "ired to be zero."
    ),
    (
        "Finite PSF radius 64 pixels; energy"
        " beyond the qualified finite refere"
        "nce and physical camera agreement r"
        "emain unestablished."
    ),
]


def _hash(p):
    return digest({k: v for k, v in p.items() if k != "profile_sha256"})


def contract(profile):
    g = profile["sensor_crops"]
    optics.validate(g)
    a = g.get("numerical_application", {})
    if a.get("status") != "passed" or a.get("policy") != direct.POLICY:
        raise ValueError("Spatial PSF requires a passed direct nine-anchor profile")
    q = a["selected"]["setting"]["samples_per_pixel"]
    radius = a["selected"]["setting"]["radius_native_px"]
    if a["capture_photons_per_pixel"] != 32000 or q != 8 or radius != 64:
        raise ValueError("Spatial v1 requires the qualified q8/r64 normalized D65 configuration")
    ids = [c["id"] for c in g["crops"]]
    if set(ids) != set(sum(TRIANGLES, [])) or len(ids) != 9:
        raise ValueError("Spatial v1 requires exactly the nine frozen sensor anchors")
    return {
        "schema": POLICY,
        "renderer_sha256": optics.sha(__file__),
        "numeric_backend": {"numpy": np.__version__, "scipy": scipy.__version__},
        "setting": deepcopy(a["selected"]["setting"]),
        "anchors": {c["id"]: c["optical_anchor_active_row_col"] for c in g["crops"]},
        "triangles": deepcopy(TRIANGLES),
        "interpolation": "nonnegative_barycentric_at_native_receiver_centers",
        "outer_boundary": "nearest_point_clamp_to_anchor_rectangle",
        "optical_halo_native_px": radius + 1,
        "isp_halo_native_px": direct.ISP_HALO,
        "tile_shape_row_col": [TILE, TILE],
        "fft_cache_max_bytes": CACHE_BYTES,
        "maximum_array_bytes": MAX_ARRAY_BYTES,
        "pixel_aperture_application_count": 1,
        "source_geometry_sha256": g["geometry_sha256"],
        "kernels": deepcopy(a["kernels"]),
        "capture_photons_per_pixel": a["capture_photons_per_pixel"],
        "scope": SCOPE,
    }


def apply(profile, result):
    p = deepcopy(profile)
    p["spatial_psf"] = deepcopy(result["application"])
    p["spatial_psf"]["validation_status"] = result["status"]
    p["profile_sha256"] = _hash(p)
    return p


def load_bank(profile, root):
    if profile.get("profile_sha256") != _hash(profile):
        raise ValueError("Spatial profile hash differs")
    c = contract(profile)
    base = Path(root).resolve()
    bank = {}
    for row in c["kernels"]:
        rel = Path(row["file"])
        path = (base / rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or not path.is_relative_to(base):
            raise ValueError("Spatial kernels must be inside the immutable result")
        if (
            row["pixel_aperture_included"]
            or row["setting"] != c["setting"]
            or optics.sha(path) != row["sha256"]
        ):
            raise ValueError("Spatial kernel application identity differs")
        with np.load(path, allow_pickle=False) as z:
            k = z["mass_rgb"]
        n = 2 * c["setting"]["samples_per_pixel"] * c["setting"]["radius_native_px"] + 1
        if (
            k.shape != (n, n, 3)
            or k.dtype != np.float32
            or not np.isfinite(k).all()
            or np.any(k < 0)
            or np.any(k.sum((0, 1), dtype=float) <= 0)
        ):
            raise ValueError("Invalid direct spatial kernel")
        bank[row["field"]] = k
    if set(bank) != set(c["anchors"]):
        raise ValueError("Missing or duplicate spatial anchor")
    return bank


def weights(application, bounds, stride=1):
    """Partition of unity on exact native sensor coordinates; at most 3 nodes/pixel."""
    y0, y1, x0, x1 = bounds
    yy, xx = np.mgrid[y0:y1:stride, x0:x1:stride].astype(float)
    anchors = application["anchors"]
    points = np.array(list(anchors.values()), float)
    yy = np.clip(yy, points[:, 0].min(), points[:, 0].max())
    xx = np.clip(xx, points[:, 1].min(), points[:, 1].max())
    xy = np.stack([yy, xx], -1)
    result = {k: np.zeros(yy.shape) for k in anchors}
    taken = np.zeros(yy.shape, bool)
    lower, upper = xy[0, 0], xy[-1, -1]
    margin = max(1.0, float(np.ptp(points, axis=0).max())) * 1e-8
    for triangle in application["triangles"]:
        p = np.array([anchors[k] for k in triangle], float)
        # A generous scale-relative margin contains the barycentric acceptance
        # tolerance. Preserve triangle order, but skip regions disjoint from
        # this tile's actual (possibly clamped) receiver-coordinate rectangle.
        if np.any(p.max(axis=0) < lower - margin) or np.any(p.min(axis=0) > upper + margin):
            continue
        uv = (xy - p[2]) @ np.linalg.inv(np.stack([p[0] - p[2], p[1] - p[2]], axis=1)).T
        bary = np.concatenate([uv, 1 - uv.sum(-1, keepdims=True)], -1)
        mask = (bary.min(-1) >= -1e-10) & ~taken
        for i, k in enumerate(triangle):
            result[k][mask] = np.clip(bary[..., i][mask], 0, 1)
        taken |= mask
    if not taken.all():
        raise ValueError("Spatial anchor triangles leave a sensor gap")
    total = sum(result.values())
    if not np.allclose(total, 1, atol=1e-12, rtol=0):
        raise ValueError("Spatial weights fail partition of unity")
    return {k: v / total for k, v in result.items() if np.any(v > 0)}


class EdgeAtlas:
    """Continuous full-image-plane chart: 9 panels with the frozen local edges."""

    def __init__(self, geometry, angle, phase):
        if angle not in (0, 90) or phase not in direct.PHASES:
            raise ValueError("Unqualified edge state")
        self.g = geometry
        self.angle = angle
        self.phase = phase
        self.rows = sorted({c["center_active_row_col"][0] for c in geometry["crops"]})
        self.cols = sorted({c["center_active_row_col"][1] for c in geometry["crops"]})

    def __call__(self, bounds, q, channel):
        y0, y1, x0, x1 = bounds
        y = 2 * q * y0 + 2 * np.arange((y1 - y0) * q, dtype=np.int64) + 1 - q
        x = 2 * q * x0 + 2 * np.arange((x1 - x0) * q, dtype=np.int64) + 1 - q
        cy = np.take(
            self.rows,
            np.searchsorted(np.array(self.rows[:-1]) + np.diff(self.rows) / 2, y / (2 * q)),
        )
        cx = np.take(
            self.cols,
            np.searchsorted(np.array(self.cols[:-1]) + np.diff(self.cols) / 2, x / (2 * q)),
        )
        across_y = y - (2 * q * cy).astype(np.int64)
        across_x = x - (2 * q * cx).astype(np.int64)
        value = (
            100 * across_x[None, :] - 7 * across_y[:, None]
            if self.angle == 0
            else 100 * across_y[:, None] + 7 * across_x[None, :]
        )
        return 0.1 + 0.7 * (value > int(200 * q * self.phase))

    def specification(self):
        return {
            "type": "continuous_9_panel_slanted_edge_atlas_v1",
            "orientation_deg": self.angle,
            "phase_native_px": self.phase,
            "slope_rational": [7, 100],
            "panel_center_rows": self.rows,
            "panel_center_cols": self.cols,
            "outside_active_sensor": "same_analytic_chart_on_extended_image_plane",
        }


class NativeRaster:
    """Native RGB image interpreted as constant radiance on each source pixel."""

    def __init__(self, values):
        v = np.asarray(values)
        if (
            v.ndim != 3
            or v.shape[-1] != 3
            or v.dtype.kind not in "fu"
            or not np.isfinite(v).all()
            or np.any(v < 0)
        ):
            raise ValueError("Native scene requires finite nonnegative RGB radiance")
        self.values = v

    def __call__(self, bounds, q, channel):
        y0, y1, x0, x1 = bounds
        y = np.clip(np.arange(y0, y1), 0, self.values.shape[0] - 1)
        x = np.clip(np.arange(x0, x1), 0, self.values.shape[1] - 1)
        return np.repeat(np.repeat(self.values[y[:, None], x[None, :], channel], q, 0), q, 1)


class FFTCache:
    def __init__(self, bank, budget=CACHE_BYTES):
        self.bank = bank
        self.budget = budget
        self.data = OrderedDict()
        self.bytes = 0
        self.peak = 0

    def kernel(self, fid, ch, shape):
        key = (fid, ch, tuple(shape))
        if key in self.data:
            self.data.move_to_end(key)
            return self.data[key]
        k = self.bank[fid][..., ch]
        pad = np.zeros(shape, float)
        n = k.shape[0]
        r = n // 2
        pad[:n, :n] = k
        pad = np.roll(pad, (-r, -r), axis=(0, 1))
        f = rfft2(pad, workers=1)
        f /= k.sum(dtype=float)
        if f.nbytes > self.budget:
            raise ValueError("One optical FFT exceeds bounded cache")
        while self.bytes + f.nbytes > self.budget:
            _, old = self.data.popitem(last=False)
            self.bytes -= old.nbytes
        self.data[key] = f
        self.bytes += f.nbytes
        self.peak = max(self.peak, self.bytes)
        return f


def estimate(application, tile=TILE):
    if type(tile) is not int or tile < 32 or tile > 192 or tile % 2:
        raise ValueError("Tile must be even in 32..192")
    q = application["setting"]["samples_per_pixel"]
    h = application["optical_halo_native_px"]
    dims = [next_fast_len((tile + 2 * h) * q)] * 2
    points = int(np.prod(dims))
    spec = dims[0] * (dims[1] // 2 + 1) * 16
    n = 2 * q * application["setting"]["radius_native_px"] + 1
    kernel_bank = 9 * n * n * 3 * 4
    bound = (
        kernel_bank + CACHE_BYTES + points * 8 * 4 + spec * 3 + 9 * tile * tile * 8 + 64 * 1024**2
    )
    if bound > MAX_ARRAY_BYTES:
        raise ValueError("Spatial optical array working set exceeds budget")
    return {
        "maximum_array_working_bytes_estimate": bound,
        "fft_shape_upper": dims,
        "kernel_bank_bytes": kernel_bank,
        "fft_cache_max_bytes": CACHE_BYTES,
        "estimate_scope": (
            "conservative live optical arrays in"
            "cluding 64MiB native/memmap allowan"
            "ce; not whole-process RSS"
        ),
    }


def tiles(bounds, tile):
    y0, y1, x0, x1 = bounds
    for y in range(y0, y1, tile):
        for x in range(x0, x1, tile):
            yield (y, min(y + tile, y1), x, min(x + tile, x1))


def convolve_tile(application, bank, cache, scene, bounds, ch, *, fixed=None):
    y0, y1, x0, x1 = bounds
    q = application["setting"]["samples_per_pixel"]
    h = application["optical_halo_native_px"]
    expanded = (y0 - h, y1 + h, x0 - h, x1 + h)
    image = np.asarray(scene(expanded, q, ch), dtype=float)
    expected = ((y1 - y0 + 2 * h) * q, (x1 - x0 + 2 * h) * q)
    if image.shape != expected or not np.isfinite(image).all() or np.any(image < 0):
        raise ValueError("Scene sampler contract differs")
    shape = tuple(next_fast_len(v) for v in image.shape)
    image_fft = rfft2(image, s=shape, workers=1)
    basis = {fixed: np.ones((y1 - y0, x1 - x0))} if fixed else weights(application, bounds)
    output = np.zeros((y1 - y0, x1 - x0), float)
    for fid, weight in basis.items():
        if fid not in bank:
            raise ValueError("Unknown direct PSF anchor")
        plane = irfft2(image_fft * cache.kernel(fid, ch, shape), s=shape, workers=1)
        area = (
            plane[h * q : (h + y1 - y0) * q, h * q : (h + x1 - x0) * q]
            .reshape(y1 - y0, q, x1 - x0, q)
            .mean(axis=(1, 3))
        )
        output += weight * area
    # Tiny FFT roundoff is never hidden for meaningful negative optical outputs.
    if output.min() < -1e-10:
        raise ValueError("Convolution produced negative optical radiance")
    return np.maximum(output, 0)


def optical_window(application, bank, scene, bounds, tile=TILE, *, fixed=None, progress=None):
    estimate(application, tile)
    cache = FFTCache(bank)
    y0, y1, x0, x1 = bounds
    result = np.empty((y1 - y0, x1 - x0, 3))
    for ch in range(3):
        for b in tiles(bounds, tile):
            a, z, c, d = b
            result[a - y0 : z - y0, c - x0 : d - x0, ch] = convolve_tile(
                application, bank, cache, scene, b, ch, fixed=fixed
            )
        if progress:
            progress(f"공간 가변 PSF · 채널 {ch + 1}/3")
    return result, cache.peak


def electrons_to_raw(profile, planes, origin=(0, 0)):
    rr, cc = np.indices(planes.shape[:2])
    site = np.array([[0, 1], [1, 2]])[(rr + origin[0]) % 2, (cc + origin[1]) % 2]
    charge = np.take_along_axis(planes, site[..., None], axis=2)[..., 0]
    local = profile["fields"]["center"]
    return core.lab._capture_electrical(
        local,
        core.lab._electrical_model(local),
        {},
        charge,
        profile["conditions"]["exposure_ms"] / 1000,
        0,
        noise_enabled=False,
    )["arrays"]["raw_dn"].astype(np.uint16)


def reflected_indices(a, b, n):
    index = np.arange(a, b)
    period = 2 * (n - 1)
    v = index % period
    return np.where(v < n, v, period - v)


def isp_window(profile, raw, bounds):
    y0, y1, x0, x1 = bounds
    h = direct.ISP_HALO
    a = (y0 - h) // 2 * 2
    c = (x0 - h) // 2 * 2
    b = (y1 + h + 1) // 2 * 2
    d = (x1 + h + 1) // 2 * 2
    frame = raw[
        reflected_indices(a, b, raw.shape[0])[:, None],
        reflected_indices(c, d, raw.shape[1])[None, :],
    ]
    linear = reference_isp.process(
        frame,
        {**profile["electronics"], "gain": profile["conditions"]["gain"]},
        profile["isp"]["electron_to_output_matrix"],
        profile["isp_tuning"],
    )[0]
    return linear[y0 - a : y1 - a, x0 - c : x1 - c]


def capture_full(profile, root, scene, out, tile=TILE, progress=None, bank=None):
    "General continuous/native-raster scene -> full odd-sized RAW/ISP via disk-backed arrays."
    root = Path(root)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    bank = load_bank(profile, root) if bank is None else bank
    a = contract(profile)
    cost = estimate(a, tile)
    g = profile["sensor_crops"]
    height, width = g["full_sensor_shape"]
    photons = np.array(g["white_e_rgb"]) * direct.PHOTON_SCALE
    planes = np.lib.format.open_memmap(
        out / "work-electrons.npy", mode="w+", dtype=float, shape=(height, width, 3)
    )
    cache = FFTCache(bank)
    elapsed = time.perf_counter()
    count = 0
    for ch in range(3):
        for bounds in tiles((0, height, 0, width), tile):
            y0, y1, x0, x1 = bounds
            planes[y0:y1, x0:x1, ch] = (
                convolve_tile(a, bank, cache, scene, bounds, ch) * photons[ch]
            )
            count += 1
            if progress and x0 == 0:
                progress(f"전체 센서 광학 · 채널 {ch + 1}/3 · 행 {y0}/{height}")
    planes.flush()
    raw = np.lib.format.open_memmap(
        out / "raw.npy", mode="w+", dtype=np.uint16, shape=(height, width)
    )
    for b in tiles((0, height, 0, width), tile):
        y0, y1, x0, x1 = b
        raw[y0:y1, x0:x1] = electrons_to_raw(profile, planes[y0:y1, x0:x1], (y0, x0))
    raw.flush()
    del planes
    (out / "work-electrons.npy").unlink()
    linear = np.lib.format.open_memmap(
        out / "linear.npy", mode="w+", dtype=float, shape=(height, width, 3)
    )
    for b in tiles((0, height, 0, width), tile):
        y0, y1, x0, x1 = b
        linear[y0:y1, x0:x1] = isp_window(profile, raw, b)
    linear.flush()
    bits = profile["electronics"]["bit_depth"]
    clipped = int(np.count_nonzero(raw >= 2**bits - 1))
    if clipped:
        raise ValueError("Spatial MTF chart clips the ADC")
    return {
        "shape_row_col": [height, width],
        "raw": str((out / "raw.npy").relative_to(root)),
        "linear": str((out / "linear.npy").relative_to(root)),
        "raw_sha256": optics.sha(out / "raw.npy"),
        "linear_sha256": optics.sha(out / "linear.npy"),
        "tile_size": tile,
        "optical_channel_tiles": count,
        "fft_cache_peak_bytes": cache.peak,
        "elapsed_s": time.perf_counter() - elapsed,
        "clipped_adc_pixels": clipped,
        **cost,
    }


def metadata(profile, crop, angle):
    p = profile["fields"]["center"]
    g = profile["sensor_crops"]
    m = core.metadata(
        p,
        {"id": crop["id"], "field": crop["id"], "chart": "mtf", "orientation": angle},
        "observation",
        0,
        profile["conditions"]["exposure_ms"] / 1000,
    )
    m["geometry"] = {
        "full_sensor_shape": g["full_sensor_shape"],
        "crop_origin": crop["origin_row_col"],
    }
    m["analysis"].pop("field_xy", None)
    m["conditions"]["incident_photons_per_pixel"] = 32000.0
    return m


def metrics(raw, linear, meta, angle, reference_raw, reference_linear):
    curves, _ = direct.curves(raw, linear, meta, angle)
    reference, _ = direct.curves(reference_raw, reference_linear, meta, angle)
    return {
        "raw_max_abs_dn": int(np.max(abs(raw.astype(int) - reference_raw.astype(int)))),
        "isp_max_abs": float(np.max(abs(linear - reference_linear))),
        "curves": curves,
        "reference_curves": reference,
        "mtf": {stage: direct.compare_curve(v, reference[stage]) for stage, v in curves.items()},
    }


def qualify(profile, direct_result, root, progress=None):
    root = Path(root)
    folder = root / "spatial-psf"
    folder.mkdir(exist_ok=False)
    a = contract(profile)
    bank = load_bank(profile, root)
    g = profile["sensor_crops"]
    cost = estimate(a)
    naive = int(np.prod(g["full_sensor_shape"])) * 3 * 8 * a["setting"]["samples_per_pixel"] ** 2
    selected = next(
        row for row in direct_result["candidates"] if row["id"] == direct_result["selected"]["id"]
    )
    captures = []
    comparisons = []
    stationary = []
    window_checks = []
    tile_check = None
    for angle in (0, 90):
        for phase in direct.PHASES:
            cid = f"edge-{angle}-phase-{int(phase * 100):02d}"
            scene = EdgeAtlas(g, angle, phase)
            if progress:
                progress("공간 가변 전체 센서 취득 · " + cid)
            full = capture_full(profile, root, scene, folder / cid, progress=progress, bank=bank)
            full.update(id=cid, scene=scene.specification())
            captures.append(full)
            raw = np.load(root / full["raw"], mmap_mode="r", allow_pickle=False)
            linear = np.load(root / full["linear"], mmap_mode="r", allow_pickle=False)
            for crop in g["crops"]:
                fid = crop["id"]
                y, x = crop["origin_row_col"]
                b = (y, y + 128, x, x + 128)
                h = direct.ISP_HALO
                extended = (y - h, y + 128 + h, x - h, x + 128 + h)
                meta = metadata(profile, crop, angle)
                # Independent output window uses another tile partition and a fresh cache.
                planes, _ = optical_window(a, bank, scene, extended, tile=96)
                planes *= np.array(g["white_e_rgb"]) * direct.PHOTON_SCALE
                window_raw = electrons_to_raw(profile, planes, (y - h, x - h))
                window_linear = reference_isp.process(
                    window_raw,
                    {**profile["electronics"], "gain": profile["conditions"]["gain"]},
                    profile["isp"]["electron_to_output_matrix"],
                    profile["isp_tuning"],
                )[0][h:-h, h:-h]
                current_raw = np.array(raw[y : y + 128, x : x + 128])
                current_linear = np.array(linear[y : y + 128, x : x + 128])
                win = metrics(
                    current_raw, current_linear, meta, angle, window_raw[h:-h, h:-h], window_linear
                )
                win["passed"] = (
                    win["raw_max_abs_dn"] == 0
                    and win["isp_max_abs"] <= LIMITS["window_isp_max_abs"]
                )
                window_checks.append({"field": fid, "chart": cid, **win})
                # Freeze one anchor to isolate FFT, aperture, CFA and ISP correctness.
                local, _ = optical_window(a, bank, scene, extended, tile=128, fixed=fid)
                local *= np.array(g["white_e_rgb"]) * direct.PHOTON_SCALE
                local_raw = electrons_to_raw(profile, local, (y - h, x - h))
                local_linear = reference_isp.process(
                    local_raw,
                    {**profile["electronics"], "gain": profile["conditions"]["gain"]},
                    profile["isp"]["electron_to_output_matrix"],
                    profile["isp_tuning"],
                )[0][h:-h, h:-h]
                saved = selected["fields"][fid]["captures"][cid]
                with np.load(root / saved["file"], allow_pickle=False) as z:
                    oldraw = z["raw"]
                    oldlinear = z["linear"]
                check = metrics(local_raw[h:-h, h:-h], local_linear, meta, angle, oldraw, oldlinear)
                check["passed"] = (
                    check["raw_max_abs_dn"] <= LIMITS["stationary_raw_max_dn"]
                    and check["isp_max_abs"] <= LIMITS["stationary_isp_max_abs"]
                    and all(
                        v["max_abs"] <= LIMITS["stationary_mtf_max_abs"]
                        and (v.get("mtf50_relative") or 0) <= LIMITS["stationary_mtf50_relative"]
                        for v in check["mtf"].values()
                    )
                )
                stationary.append({"field": fid, "chart": cid, **check})
                change = metrics(current_raw, current_linear, meta, angle, oldraw, oldlinear)
                name = folder / (fid + "-" + cid + ".npz")
                np.savez_compressed(
                    name,
                    raw=current_raw,
                    linear=current_linear,
                    stationary_raw=oldraw,
                    stationary_linear=oldlinear,
                    window_raw=window_raw[h:-h, h:-h],
                    window_linear=window_linear,
                )
                comparisons.append(
                    {
                        "field": fid,
                        "chart": cid,
                        "metadata": meta,
                        "file": str(name.relative_to(root)),
                        "sha256": optics.sha(name),
                        **change,
                    }
                )
            # Compare every active pixel with another tile partition, including odd borders.
            if tile_check is None:
                alt = capture_full(
                    profile,
                    root,
                    scene,
                    folder / "partition-96",
                    tile=96,
                    progress=progress,
                    bank=bank,
                )
                alternate_raw = np.load(root / alt["raw"], mmap_mode="r", allow_pickle=False)
                alternate_linear = np.load(root / alt["linear"], mmap_mode="r", allow_pickle=False)
                rd = 0
                ld = 0.0
                for b in tiles((0, raw.shape[0], 0, raw.shape[1]), 128):
                    y0, y1, x0, x1 = b
                    rd = max(
                        rd,
                        int(
                            np.max(
                                abs(
                                    raw[y0:y1, x0:x1].astype(int)
                                    - alternate_raw[y0:y1, x0:x1].astype(int)
                                )
                            )
                        ),
                    )
                    ld = max(
                        ld,
                        float(np.max(abs(linear[y0:y1, x0:x1] - alternate_linear[y0:y1, x0:x1]))),
                    )
                tile_check = {
                    "partitions": [128, 96],
                    "active_pixels_compared": int(raw.size),
                    "raw_max_abs_dn": rd,
                    "isp_max_abs": ld,
                    "passed": rd == 0 and ld <= LIMITS["tile_isp_max_abs"],
                    "alternate": alt,
                }
    passed = tile_check["passed"] and all(c["passed"] for c in window_checks + stationary)
    result = {
        "schema": POLICY,
        "status": "passed" if passed else "not_validated",
        "application": a,
        "limits": LIMITS,
        "scope": SCOPE,
        "captures": captures,
        "comparisons": comparisons,
        "stationary_renderer_checks": stationary,
        "independent_window_checks": window_checks,
        "partition_check": tile_check,
        "resource": {
            **cost,
            "naive_full_supersampled_rgb_single_buffer_bytes": naive,
            "naive_to_tiled_array_ratio": naive / cost["maximum_array_working_bytes_estimate"],
        },
        "crop_change_scope": (
            "descriptive variation from old stationary anchors; not a numerical parity gate"
        ),
        "physical_validation": "not_established",
    }
    optics.save(folder / "result.json", result)
    return result


def capture_applied(profile, root, scene, out, tile=TILE, progress=None):
    """Execute a frozen, validated full-sensor profile, refusing changed renderer contracts."""
    if "boundary_spatial_psf" in profile:
        from .calibration_anchor_density import capture_applied as boundary_capture

        return boundary_capture(profile, root, scene, out, tile, progress)
    if profile.get("profile_sha256") != _hash(profile):
        raise ValueError("Applied spatial profile hash differs")
    frozen = deepcopy(profile.get("spatial_psf", {}))
    if frozen.pop("validation_status", None) != "passed" or frozen != contract(profile):
        raise ValueError("Applied spatial policy or renderer differs from validated profile")
    return capture_full(profile, root, scene, out, tile, progress)
