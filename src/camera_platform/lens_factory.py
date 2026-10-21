"""Bounded reciprocal ray tracing for explicitly declared, ideal spherical optics.

This module samples image-space radiance, not forward pupil counts. It never
imports a GUI model, fetches a glass catalog, infers apertures, or approves a lens.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import time
from collections.abc import Mapping

import numpy as np

from . import finite_conjugate, lens_direction_response, optics_surfaces, pupil_transmission

FACTORY_ID = "rayoptics_reciprocal_spherical_v1"
MAX_RAYS = 65_536
MAX_INTERSECTIONS = 1_000_000
MAX_SECONDS = 45.0


class LensFactoryError(ValueError):
    def __init__(self, path, message, code="INVALID_PRESCRIPTION"):
        self.issues = [{"code": code, "path": path, "message": message}]
        super().__init__(f"{path}: {message}")


def _fail(path, message, code="INVALID_PRESCRIPTION"):
    raise LensFactoryError(path, message, code)


def _keys(value, allowed, path):
    if not isinstance(value, dict) or set(value) != set(allowed):
        _fail(path, f"Declare exactly these supported keys: {', '.join(sorted(allowed))}")


def _number(value, path, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        _fail(path, f"Expected a finite number in [{low}, {high}]")
    return float(value)


def _axis(value, path, low, high, minimum=2, maximum=32):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        _fail(path, f"Expected {minimum}..{maximum} explicitly declared samples")
    values = [_number(v, f"{path}[{i}]", low, high) for i, v in enumerate(value)]
    if any(b <= a for a, b in zip(values[:-1], values[1:], strict=True)):
        _fail(path, "Samples must increase strictly")
    return values


def _sag(radius, radial):
    if radius is None:
        return 0.0
    # Stable spherical vertex sag; validation requires aperture < |radius|.
    return math.copysign(radial**2 / (abs(radius) + math.sqrt(radius**2 - radial**2)), radius)


def _angular_support(data, rear_distance):
    """Bound an equal-area image-space disk without omitting admitted rays.

    A centered plane-parallel tail conserves ``n sin(theta)`` and the
    transverse azimuth. If both the sensor and the interval after a preceding
    clear surface are air, an admitted ray has the *same* angle in that air
    interval as at the sensor. Its lateral travel there alone cannot exceed
    the sum of the field height and that surface's clear semi-diameter.
    Every other tail segment adds travel in the same transverse direction.
    This geometric proof depends on the declared apertures and materials;
    it does not verify their physical accuracy or lens transmission.
    """
    height = data["image_height_mm"][-1]
    rear = data["surfaces"][-1]
    full_radius = max(rear["semi_diameter_mm"], height)
    full_theta = math.degrees(math.atan2(height + full_radius, rear_distance))
    if full_theta < 80 or data["schema"] not in {"lens_prescription_v4", "lens_prescription_v5"}:
        return {
            "disk_radius_mm": full_radius,
            "maximum_sample_angle_deg": full_theta,
            "certificate": None,
        }
    best = None
    surfaces = data["surfaces"]
    for index in range(len(surfaces) - 1):
        surface = surfaces[index]
        tail = surfaces[index + 1 :]
        if (
            surface["refractive_index_after"] != [1.0] * len(data["wavelength_nm"])
            or any(item["profile"] != "plane" for item in tail)
        ):
            continue
        sag_upper = optics_surfaces.surface_bounds(surface)["sag_max_mm"]
        air_gap = surface["thickness_mm"] - max(0.0, sag_upper)
        if air_gap <= 1e-5:
            continue
        theta_bound = math.degrees(
            math.atan2(height + surface["semi_diameter_mm"], air_gap)
        )
        radius = rear_distance * math.tan(math.radians(theta_bound))
        theta_sample = theta_bound
        if theta_sample >= 80 or radius >= full_radius:
            continue
        candidate = {
            "disk_radius_mm": radius,
            "maximum_sample_angle_deg": theta_sample,
            "certificate": {
                "schema": "camera_planar_air_tail_support_v3",
                "limiting_surface_id": surface["id"],
                "limiting_surface_index": index,
                "limiting_clear_semi_diameter_mm": surface["semi_diameter_mm"],
                "minimum_air_gap_mm": air_gap,
                "maximum_admitted_sensor_angle_deg": theta_bound,
                "full_rear_disk_radius_mm": full_radius,
                "full_rear_disk_maximum_angle_deg": full_theta,
                "sampling_disk_radius_mm": radius,
                "sampling_disk_center": "sensor_field_point_on_rear_enclosing_plane",
                "sampling_maximum_angle_deg": theta_sample,
                "proof_scope": (
                    "centered_plane_parallel_tail_with_air_at_sensor_and_after_"
                    "limiting_surface_and_declared_clear_aperture"
                ),
                "physical_validation": "not_established",
            },
        }
        if best is None or radius < best["disk_radius_mm"]:
            best = candidate
    return best or {
        "disk_radius_mm": full_radius,
        "maximum_sample_angle_deg": full_theta,
        "certificate": None,
    }


def _validate_request(request: Mapping, *, require_direction_domain: bool) -> dict:
    """Normalize the prescription, optionally checking angular LUT support."""
    try:
        data = json.loads(json.dumps(request, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise LensFactoryError("request", "A finite JSON object is required") from exc
    v5 = isinstance(data, dict) and data.get("schema") == "lens_prescription_v5"
    v4 = isinstance(data, dict) and data.get("schema") in (
        "lens_prescription_v4", "lens_prescription_v5",
    )
    v3 = isinstance(data, dict) and data.get("schema") in (
        "lens_prescription_v3",
        "lens_prescription_v4",
        "lens_prescription_v5",
    )
    v2 = isinstance(data, dict) and data.get("schema") in (
        "lens_prescription_v2",
        "lens_prescription_v3",
        "lens_prescription_v4",
        "lens_prescription_v5",
    )
    _keys(
        data,
        {
            "schema",
            "length_unit",
            "wavelength_unit",
            "wavelength_nm",
            "surfaces",
            "field_radius",
            "image_height_mm",
            "illumination",
            "sampling",
        }
        | ({"stop_transmission"} if v2 else set())
        | ({"object_conjugate"} if v5 else set()),
        "request",
    )
    if (data["schema"], data["length_unit"], data["wavelength_unit"]) != (
        (
            "lens_prescription_v5"
            if v5
            else "lens_prescription_v4"
            if v4
            else "lens_prescription_v3"
            if v3
            else "lens_prescription_v2"
            if v2
            else "lens_prescription_v1"
        ),
        "mm",
        "nm",
    ):
        _fail("request", "Use lens_prescription_v1 through v5 with explicit mm and nm units")
    wave = _axis(data["wavelength_nm"], "wavelength_nm", 350, 1100, maximum=16)
    fields = _axis(data["field_radius"], "field_radius", 0, 1, maximum=17)
    heights = _axis(data["image_height_mm"], "image_height_mm", 0, 500, maximum=17)
    if len(fields) != len(heights) or fields[0] != 0 or fields[-1] != 1 or heights[0] != 0:
        _fail("field_radius", "Cover center=0 through edge=1 with matching physical image heights")
    if not np.allclose(fields, np.asarray(heights) / heights[-1], rtol=0, atol=1e-12):
        _fail("field_radius", "Normalized radius must equal image_height_mm / maximum height")
    illumination = {
        "kind": "uniform_object_space_radiance",
        "transmission": "explicit_stop_transmission" if v2 else "ideal_lossless",
        "object_medium": "air",
        "image_medium": "air",
    }
    if data["illumination"] != illumination:
        _fail(
            "illumination",
            "Only uniform object-space radiance, ideal lossless optics, "
            "air at both ends are supported",
        )
    surfaces = data["surfaces"]
    if not isinstance(surfaces, list) or not 1 <= len(surfaces) <= optics_surfaces.MAX_SURFACES:
        _fail(
            "surfaces",
            f"Declare 1..{optics_surfaces.MAX_SURFACES} ordered physical surfaces "
            "from object toward sensor",
        )
    identifiers = set()
    stop_count = 0
    previous_index = [1.0] * len(wave)
    for i, surface in enumerate(surfaces):
        path = f"surfaces[{i}]"
        _keys(
            surface,
            {
                "id",
                "profile",
                "radius_mm",
                "thickness_mm",
                "semi_diameter_mm",
                "refractive_index_after",
                "is_stop",
            }
            | (
                {"asphere"}
                if v4 and isinstance(surface, dict) and surface.get("profile") == "even_asphere"
                else set()
            ),
            path,
        )
        if not isinstance(surface["id"], str) or not 1 <= len(surface["id"]) <= 100:
            _fail(path + ".id", "Declare a nonempty surface identifier of at most 100 characters")
        if surface["id"] in identifiers:
            _fail(path + ".id", "Surface identifiers must be unique")
        identifiers.add(surface["id"])
        radius = surface["radius_mm"]
        aperture = _number(surface["semi_diameter_mm"], path + ".semi_diameter_mm", 1e-4, 500)
        surface["semi_diameter_mm"] = aperture
        if surface["profile"] == "spherical":
            radius = _number(radius, path + ".radius_mm", -1e6, 1e6)
            if abs(radius) <= aperture:
                _fail(
                    path + ".radius_mm",
                    "The clear aperture must lie strictly inside the spherical hemisphere",
                )
        elif v4 and surface["profile"] == "even_asphere":
            try:
                surface["asphere"] = optics_surfaces.validate_asphere(surface)
            except ValueError as exc:
                _fail(path + ".asphere", str(exc))
            radius = float(radius)
        elif surface["profile"] != "plane" or radius is not None:
            _fail(
                path + ".profile",
                "Use spherical with signed radius or plane with radius_mm=null; "
                "aspheres/tilt/decenter are unsupported",
            )
        surface["radius_mm"] = radius
        surface["thickness_mm"] = _number(
            surface["thickness_mm"], path + ".thickness_mm", 1e-5, 5000
        )
        index = surface["refractive_index_after"]
        if not isinstance(index, list) or len(index) != len(wave):
            _fail(
                path + ".refractive_index_after",
                "Declare one absolute real index per exact wavelength; no dispersion interpolation",
            )
        index = [_number(n, path + ".refractive_index_after", 1, 3) for n in index]
        surface["refractive_index_after"] = index
        if type(surface["is_stop"]) is not bool:
            _fail(path + ".is_stop", "Expected an explicit boolean")
        if surface["is_stop"]:
            stop_count += 1
            if radius is not None or index != previous_index:
                _fail(path, "The explicit stop must be an unpowered plane with unchanged medium")
        previous_index = index
    if stop_count != 1 or previous_index != [1.0] * len(wave):
        _fail("surfaces", "Require exactly one explicit plane stop and air after the final surface")
    if v2:
        try:
            data["stop_transmission"] = pupil_transmission.bind_transmission(
                data["stop_transmission"], data
            )
        except ValueError as exc:
            raise LensFactoryError("stop_transmission", str(exc)) from exc
    if sum(s["thickness_mm"] for s in surfaces) > 5000:
        _fail("surfaces", "Total axial track exceeds 5000 mm")
    if v4:
        try:
            geometry = optics_surfaces.certify_prescription(surfaces)
        except ValueError as exc:
            _fail("surfaces", str(exc), "UNCERTIFIED_GEOMETRY")
        if v5:
            try:
                data["object_conjugate"] = finite_conjugate.validate_conjugate(
                    data["object_conjugate"], surfaces
                )
                finite_conjugate.object_xy_mm([heights[-1], 0], data["object_conjugate"])
            except ValueError as exc:
                _fail("object_conjugate", str(exc), "UNCERTIFIED_GEOMETRY")
    else:
        for i, (front, rear) in enumerate(zip(surfaces[:-1], surfaces[1:], strict=True)):
            radius = min(front["semi_diameter_mm"], rear["semi_diameter_mm"])
            edge_gap = (
                front["thickness_mm"]
                + _sag(rear["radius_mm"], radius)
                - _sag(front["radius_mm"], radius)
            )
            if edge_gap <= 1e-6:
                _fail(
                    f"surfaces[{i}]",
                    "Adjacent spherical surfaces intersect inside their common clear aperture",
                )
    rear = surfaces[-1]
    if v4:
        distance = geometry["rear_plane_distance_mm"]
    else:
        rear_sag = max(0.0, _sag(rear["radius_mm"], rear["semi_diameter_mm"]))
        distance = rear["thickness_mm"] - rear_sag
    if distance <= 1e-5:
        _fail(
            "surfaces[-1].thickness_mm",
            "Sensor plane must lie strictly behind the entire rear clear surface",
        )
    # Every segment to the rear surface crosses the enclosing plane in a convex
    # combination of the sensor point and a point in the rear clear disk.
    support = _angular_support(data, distance)
    maximum_theta = support["maximum_sample_angle_deg"]
    if require_direction_domain and maximum_theta >= 80:
        _fail(
            "image_height_mm",
            "Conservative image-space angular support must remain below 80 degrees",
        )
    sampling = data["sampling"]
    _keys(
        sampling,
        {
            "polar_rings",
            "azimuth_samples",
            "theta_bins",
            "ri_tolerance_absolute",
            "angular_cdf_tolerance_absolute",
        }
        | ({"irradiance_tolerance_relative"} if v2 else set())
        | ({"on_axis_azimuth_samples"} if v3 else set()),
        "sampling",
    )
    for key, low, high in (
        ("polar_rings", 4, 128),
        ("azimuth_samples", 8, 256),
        ("theta_bins", 8, 128),
    ):
        if type(sampling[key]) is not int or not low <= sampling[key] <= high:
            _fail("sampling." + key, f"Expected an integer in [{low}, {high}]")
    for key in ("ri_tolerance_absolute", "angular_cdf_tolerance_absolute"):
        sampling[key] = _number(sampling[key], "sampling." + key, 1e-6, 0.1)
    if v2:
        sampling["irradiance_tolerance_relative"] = _number(
            sampling["irradiance_tolerance_relative"],
            "sampling.irradiance_tolerance_relative",
            1e-6,
            0.03,
        )
    rays = len(wave) * len(fields) * sampling["polar_rings"] * sampling["azimuth_samples"] * 5
    if rays > MAX_RAYS or rays * (len(surfaces) + 1 + int(v5)) > MAX_INTERSECTIONS:
        _fail(
            "sampling",
            "Coarse + refined work exceeds 65,536 rays or 1,000,000 interface visits",
            "RESOURCE_LIMIT",
        )
    if v3:
        azimuths = sampling["on_axis_azimuth_samples"]
        if type(azimuths) is not int or not 8 <= azimuths <= 128 or azimuths % 4:
            _fail("sampling.on_axis_azimuth_samples", "Declare 8..128 samples, a multiple of four")
        samples = 4 * sampling["polar_rings"] * sampling["azimuth_samples"]
        if samples > lens_direction_response.MAX_BASE_SAMPLES:
            _fail(
                "sampling", "Joint direction fine-pass base samples exceed 8192", "RESOURCE_LIMIT"
            )
        expanded = len(wave) * samples * (azimuths + len(fields) - 1)
        if expanded > lens_direction_response.MAX_EXPANDED_SAMPLES:
            _fail(
                "sampling",
                "Joint direction azimuth expansion exceeds one million samples",
                "RESOURCE_LIMIT",
            )
    data.update(wavelength_nm=wave, field_radius=fields, image_height_mm=heights)
    return data


def validate_request(request: Mapping) -> dict:
    """Validate the supported directional-radiometry Factory contract."""
    return _validate_request(request, require_direction_domain=True)


def validate_wavefront_prescription(request: Mapping) -> dict:
    """Validate physical surfaces for OPD tracing without the LUT angle bound.

    The full rear-plane quadrature's 80-degree bound belongs to directional
    radiometry.  OPD tracing uses entrance-pupil rays and enforces its own cone,
    phase, aperture, convergence and acceptance limits downstream.  This does
    not authorize a lens-direction or radiometric characterization.
    """
    return _validate_request(request, require_direction_domain=False)


def describe_capabilities() -> dict:
    available = importlib.util.find_spec("rayoptics") is not None
    return {
        "id": FACTORY_ID,
        "available": available,
        "backend_version": importlib.metadata.version("rayoptics") if available else None,
        "supported": [
            "centered_spherical_and_plane_surfaces",
            "centered_even_asphere_A4_A6_A8_v4_or_v5",
            "centered_even_asphere_A10_explicit_v2_contract",
            "centered_even_asphere_A12_explicit_v3_contract",
            "certified_planar_air_tail_directional_quadrature",
            "explicit_spectral_indices",
            "geometric_vignetting",
            "spectral_incidence_distribution",
            "ideal_geometric_relative_irradiance",
            "finite_planar_object_conjugate_v5",
        ],
        "unsupported": [
            "tilt_decenter",
            "diffraction_psf_mtf",
            "fresnel_or_coating_loss",
            "absorption",
            "stray_light",
            "arbitrary_3d_scene_radiance_transport",
            "physical_validation",
        ],
        "limits": {
            "surfaces": optics_surfaces.MAX_SURFACES,
            "rays": MAX_RAYS,
            "interface_visits": MAX_INTERSECTIONS,
            "wall_seconds": MAX_SECONDS,
        },
    }


def _backend():
    try:
        from rayoptics.elem.profiles import Spherical
        from rayoptics.elem.surface import Surface
        from rayoptics.raytr.raytrace import trace_raw
        from rayoptics.raytr.traceerror import (
            TraceMissedSurfaceError,
            TraceRayBlockedError,
            TraceTIRError,
        )
    except ImportError as exc:
        raise LensFactoryError(
            "backend", "Install the optional RayOptics 0.9.8 factory dependency", "MISSING_BACKEND"
        ) from exc
    if importlib.metadata.version("rayoptics") != "0.9.8":
        _fail(
            "backend", "This adapter is verified with RayOptics 0.9.8 only", "UNSUPPORTED_BACKEND"
        )
    return (
        Spherical,
        Surface,
        trace_raw,
        (TraceRayBlockedError, TraceTIRError, TraceMissedSurfaceError),
    )


def _paths(data, backend):
    spherical, surface, _, _ = backend
    surfaces = data["surfaces"]
    vertices = np.r_[0.0, np.cumsum([s["thickness_mm"] for s in surfaces])]
    interfaces = [
        surface(
            profile=(
                optics_surfaces.profile(s)
                if data["schema"] in {"lens_prescription_v4", "lens_prescription_v5"}
                else spherical(c=0.0)
                if s["radius_mm"] is None
                else spherical(r=s["radius_mm"])
            ),
            max_ap=s["semi_diameter_mm"],
        )
        for s in surfaces
    ]
    image = surface(profile=spherical(c=0.0), interact_mode="dummy")
    paths = []
    for w in range(len(data["wavelength_nm"])):
        indexes = [1.0] + [s["refractive_index_after"][w] for s in surfaces]
        path = [
            (image, None, (np.eye(3), np.array([0.0, 0.0, -surfaces[-1]["thickness_mm"]])), 1.0, -1)
        ]
        for i in reversed(range(len(surfaces))):
            translation = np.array([0.0, 0.0, vertices[i - 1] - vertices[i]]) if i else np.zeros(3)
            path.append((interfaces[i], None, (np.eye(3), translation), indexes[i], -1))
        paths.append(path)
    return paths


def _sampling(data, refinement):
    settings = data["sampling"]
    nr, na = settings["polar_rings"] * refinement, settings["azimuth_samples"] * refinement
    rear = data["surfaces"][-1]
    if data["schema"] in {"lens_prescription_v4", "lens_prescription_v5"}:
        rear_sag = max(0.0, optics_surfaces.surface_bounds(rear)["sag_max_mm"])
        distance = math.nextafter(rear["thickness_mm"] - rear_sag, -math.inf)
    else:
        rear_sag = max(0.0, _sag(rear["radius_mm"], rear["semi_diameter_mm"]))
        distance = rear["thickness_mm"] - rear_sag
    support = _angular_support(data, distance)
    companded = support["certificate"] is not None
    # The certified disk is complete, but almost all of a wide rear disk can
    # still be vignetted. Squared radial edges allocate more annuli near the
    # field point without discarding the outer region; weights are exact
    # annular areas, not an equal-weight approximation.
    if companded:
        edges = (np.arange(nr + 1, dtype=float) / nr) ** 2
        radius = np.sqrt((edges[:-1] ** 2 + edges[1:] ** 2) / 2)
        area_fraction = np.repeat(np.diff(edges**2) / na, na)
    else:
        radius = np.sqrt((np.arange(nr) + 0.5) / nr)
        area_fraction = np.full(nr * na, 1 / (nr * na))
    azimuth = 2 * np.pi * (np.arange(na) + 0.5) / na
    disk = np.stack(
        np.broadcast_arrays(radius[:, None] * np.cos(azimuth), radius[:, None] * np.sin(azimuth)),
        axis=-1,
    ).reshape(-1, 2)
    positions, directions, weights = [], [], []
    for height in data["image_height_mm"]:
        bound = (
            support["disk_radius_mm"]
            if support["certificate"] is not None
            else max(height, rear["semi_diameter_mm"])
        )
        if height == 0:
            # Centered spherical optics are exactly rotation invariant on axis.
            # Integrate each complete annulus analytically over azimuth, using
            # the same ray budget for radial refinement at aperture boundaries.
            if companded:
                on_axis_edges = (np.arange(nr * na + 1, dtype=float) / (nr * na)) ** 2
                radii = np.sqrt((on_axis_edges[:-1] ** 2 + on_axis_edges[1:] ** 2) / 2)
                sample_area_fraction = np.diff(on_axis_edges**2)
            else:
                radii = np.sqrt((np.arange(nr * na) + 0.5) / (nr * na))
                sample_area_fraction = area_fraction
            xy = np.column_stack((radii * bound, np.zeros(nr * na)))
        else:
            xy = disk * bound
            if companded:
                xy[:, 0] += height
            sample_area_fraction = area_fraction
        delta = np.column_stack((xy[:, 0] - height, xy[:, 1], np.full(len(xy), -distance)))
        direction = delta / np.linalg.norm(delta, axis=1)[:, None]
        positions.append(xy)
        directions.append(direction)
        if companded:
            weights.append(
                np.pi * bound**2 * sample_area_fraction * direction[:, 2] ** 4 / distance**2
            )
        else:
            weights.append(np.pi * bound**2 / len(xy) * direction[:, 2] ** 4 / distance**2)
    return np.array(positions), np.array(directions), np.array(weights), distance


def _pass(data, backend, paths, theta_axis, refinement, deadline):
    _, _, trace, errors = backend
    positions, directions, weights, distance = _sampling(data, refinement)
    shape = (len(paths), len(directions), directions.shape[1])
    accepted = np.zeros(shape, dtype=bool)
    exit_directions = np.zeros((*shape, 3))
    finite = data["schema"] == "lens_prescription_v5"
    if finite:
        exit_positions = np.zeros((*shape, 3))
        object_xy = np.zeros((*shape, 2))
        object_distance = np.zeros(shape)
        object_valid = np.zeros(shape, dtype=bool)
    counts = np.zeros((*shape[:2], 4), dtype=np.int64)
    transmission = data.get("stop_transmission")
    if transmission is not None:
        stop_index = next(i for i, s in enumerate(data["surfaces"]) if s["is_stop"])
        stop = data["surfaces"][stop_index]
        # The reciprocal path is image, last physical surface, ..., first.
        stop_ray_index = len(data["surfaces"]) - stop_index
        stop_xy = np.zeros((*shape, 2))
        power_transmittance = np.zeros(shape)
    for w, path in enumerate(paths):
        for f, ray_directions in enumerate(directions):
            point = np.array([data["image_height_mm"][f], 0.0, 0.0])
            for n, direction in enumerate(ray_directions):
                if n % 32 == 0 and time.monotonic() > deadline:
                    _fail(
                        "sampling", "Factory exceeded its 45 second compute deadline", "TIME_LIMIT"
                    )
                try:
                    ray, _, _ = trace(
                        iter(path),
                        point,
                        direction,
                        data["wavelength_nm"][w],
                        check_apertures=True,
                        intersect_obj=False,
                        pt_inside_fuzz=0.0,
                    )
                except optics_surfaces.AsphereIntersectionError:
                    counts[w, f, 3] += 1
                    continue
                except errors as exc:
                    counts[
                        w, f, next(i for i, error in enumerate(errors) if isinstance(exc, error))
                    ] += 1
                    continue
                # Negative travel or non-finite geometry is a solver/model issue,
                # not a physically blocked ray and never silently accepted.
                if len(ray) != len(path) or any(
                    segment[2] < -1e-8
                    or not np.isfinite(np.r_[segment[0], segment[1], segment[2]]).all()
                    for segment in ray
                ):
                    counts[w, f, 3] += 1
                    continue
                if data["schema"] in {"lens_prescription_v4", "lens_prescription_v5"}:
                    try:
                        for index, physical in enumerate(reversed(data["surfaces"]), 1):
                            optics_surfaces.validate_intersection(physical, ray[index][0])
                    except optics_surfaces.AsphereIntersectionError:
                        counts[w, f, 3] += 1
                        continue
                if finite:
                    try:
                        hit, travel = finite_conjugate.intersect_object_plane(
                            ray[-1][0], ray[-1][1], data["object_conjugate"]
                        )
                    except ValueError:
                        counts[w, f, 3] += 1
                        continue
                    exit_positions[w, f, n] = ray[-1][0]
                    object_xy[w, f, n] = hit[:2]
                    object_distance[w, f, n] = travel
                    object_valid[w, f, n] = True
                accepted[w, f, n] = True
                exit_directions[w, f, n] = ray[-1][1]
                if transmission is not None:
                    stop_point = ray[stop_ray_index][0]
                    power_transmittance[w, f, n] = pupil_transmission.power_at_stop(
                        transmission,
                        data["wavelength_nm"][w],
                        stop_point,
                        stop["semi_diameter_mm"],
                    )
                    stop_xy[w, f, n] = stop_point[:2]
    angles = np.degrees(np.arccos(np.clip(-directions[:, :, 2], 0, 1)))
    angular = np.zeros((*shape[:2], len(theta_axis)))
    for f in range(shape[1]):
        upper = np.clip(
            np.searchsorted(theta_axis, angles[f], side="right"), 1, len(theta_axis) - 1
        )
        fraction = (angles[f] - theta_axis[upper - 1]) / (theta_axis[upper] - theta_axis[upper - 1])
        for w in range(shape[0]):
            power = weights[f] * accepted[w, f]
            if transmission is not None:
                power = power * power_transmittance[w, f]
            np.add.at(angular[w, f], upper - 1, power * (1 - fraction))
            np.add.at(angular[w, f], upper, power * fraction)
    integrals = angular.sum(axis=-1)
    conditional = np.divide(
        angular, integrals[:, :, None], out=np.zeros_like(angular), where=integrals[:, :, None] > 0
    )
    relative = np.divide(
        integrals, integrals[:, :1], out=np.zeros_like(integrals), where=integrals[:, :1] > 0
    )
    result = {
        "irradiance_per_radiance_sr": integrals,
        "relative_illumination": relative,
        "angular_weights": conditional,
        "accepted_rays": accepted,
        "failure_counts": counts,
        "object_exit_direction_cosines": exit_directions,
        "image_direction_cosines": -directions,
        "plane_sample_xy_mm": positions,
        "quadrature_weights_sr": weights,
        "sampling_plane_distance_mm": distance,
    }
    if transmission is not None:
        result.update(stop_intersection_xy_mm=stop_xy, stop_power_transmittance=power_transmittance)
    if finite:
        result.update(
            object_exit_position_xyz_mm=exit_positions,
            object_plane_xy_mm=object_xy,
            object_plane_ray_distance_mm=object_distance,
            object_plane_valid=object_valid,
        )
    return result


def characterize(request: Mapping) -> dict:
    """Trace two deterministic quadrature levels; return arrays and a gated LUT."""
    data = validate_request(request)
    backend = _backend()
    deadline = time.monotonic() + MAX_SECONDS
    paths = _paths(data, backend)
    _, _, _, distance = _sampling(data, 1)
    support = _angular_support(data, distance)
    maximum_theta = support["maximum_sample_angle_deg"]
    theta = np.linspace(0, maximum_theta, data["sampling"]["theta_bins"] + 1)
    coarse = _pass(data, backend, paths, theta, 1, deadline)
    fine = _pass(data, backend, paths, theta, 2, deadline)
    ri_delta = np.abs(fine["relative_illumination"] - coarse["relative_illumination"])
    cdf_delta = np.max(
        np.abs(np.cumsum(fine["angular_weights"] - coarse["angular_weights"], axis=-1)), axis=-1
    )
    integral_delta = np.abs(
        fine["irradiance_per_radiance_sr"] - coarse["irradiance_per_radiance_sr"]
    )
    positive = bool(
        (fine["irradiance_per_radiance_sr"] > 0).all()
        and (coarse["irradiance_per_radiance_sr"] > 0).all()
    )
    geometry_valid = not bool(
        fine["failure_counts"][:, :, 3].any() or coarse["failure_counts"][:, :, 3].any()
    )
    converged = bool(
        positive
        and geometry_valid
        and np.max(ri_delta) <= data["sampling"]["ri_tolerance_absolute"]
        and np.max(cdf_delta) <= data["sampling"]["angular_cdf_tolerance_absolute"]
    )
    integral_relative_delta = np.divide(
        integral_delta,
        fine["irradiance_per_radiance_sr"],
        out=np.zeros_like(integral_delta),
        where=fine["irradiance_per_radiance_sr"] > 0,
    )
    if "stop_transmission" in data:
        converged = converged and bool(
            np.max(integral_relative_delta) <= data["sampling"]["irradiance_tolerance_relative"]
        )
    payload = {
        "wavelength_nm": data["wavelength_nm"],
        "field_radius": data["field_radius"],
        "theta_deg": theta.tolist(),
        "angular_weights": fine["angular_weights"].tolist(),
        "relative_illumination": fine["relative_illumination"].tolist(),
    }
    relative_defined = fine["irradiance_per_radiance_sr"][:, 0] > 0
    angular_defined = fine["irradiance_per_radiance_sr"] > 0
    summary = {
        "schema": "lens_characterization_v1",
        "factory_id": FACTORY_ID,
        "status": "PROPOSED" if converged else "REFUSED",
        "source_kind": "simulated",
        "backend": {"name": "rayoptics", "version": importlib.metadata.version("rayoptics")},
        "method_and_version": FACTORY_ID,
        "physical_validation": "not_established",
        "approval_status": "unapproved",
        "uncertainty": {"kind": "unknown"},
        "normalized_request": data,
        "request_sha256": hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
        "included_effects": ["lens_relative_illumination", "lens_incident_angle_distribution"],
        "reference_plane": "sensor_surface",
        "declared_sampling_coverage_diameter_mm": 2 * data["image_height_mm"][-1],
        "image_circle_quality_validation": "not_established",
        "wavelength_nm": data["wavelength_nm"],
        "field_radius": data["field_radius"],
        "image_height_mm": data["image_height_mm"],
        "theta_deg": theta.tolist(),
        "relative_illumination": [
            row if relative_defined[w] else [None] * len(row)
            for w, row in enumerate(payload["relative_illumination"])
        ],
        "normalization_defined": {
            "relative_illumination_per_wavelength": relative_defined.tolist(),
            "angular_distribution_per_field": angular_defined.tolist(),
        },
        "irradiance_per_radiance_sr": fine["irradiance_per_radiance_sr"].tolist(),
        "mean_incidence_angle_deg": np.sum(fine["angular_weights"] * theta, axis=-1).tolist(),
        "sampling": {
            "measure": (
                "squared_radial_annuli_certified_planar_tail_sensor_centered_disk"
                if support["certificate"] is not None
                else "equal_area_enclosing_rear_plane_disk"
            ),
            "power_weight": (
                "sample_annular_area * cos(theta)^4 / plane_distance^2"
                if support["certificate"] is not None
                else "disk_area/sample_count * cos(theta)^4 / plane_distance^2"
            ),
            "integral_unit": "sr",
            "plane_distance_mm": distance,
            "bounds": (
                "A certified plane-parallel air tail bounds every admitted sensor angle; "
                "the reduced disk still encloses every admitted segment"
                if support["certificate"] is not None
                else "Plane at rear maximum sag; radius=max(rear aperture radius, field height) "
                "encloses every connecting segment"
            ),
            "angular_basis": "linear allocation onto theta nodes; no spectral interpolation",
            "on_axis_measure": (
                "Each center-field ray represents a complete squared-edge annulus; "
                "its exact area weight integrates azimuth by declared rotational symmetry"
                if support["certificate"] is not None
                else "Each center-field ray represents a complete equal-area annulus; "
                "azimuth integrates analytically by declared rotational symmetry"
            ),
            "coarse_rays": int(coarse["accepted_rays"].size),
            "fine_rays": int(fine["accepted_rays"].size),
            "failure_categories": [
                "aperture_blocked",
                "total_internal_reflection",
                "missed_surface",
                "invalid_trace",
            ],
            "coarse_failure_counts": coarse["failure_counts"].tolist(),
            "fine_failure_counts": fine["failure_counts"].tolist(),
            "coarse_accepted_count": coarse["accepted_rays"].sum(axis=-1).tolist(),
            "fine_accepted_count": fine["accepted_rays"].sum(axis=-1).tolist(),
        },
        "convergence": {
            "observed_two_level_agreement": converged,
            "relative_illumination_absolute_change": ri_delta.tolist(),
            "angular_cdf_max_absolute_change": cdf_delta.tolist(),
            "irradiance_integral_absolute_change_sr": integral_delta.tolist(),
            "thresholds": copy.deepcopy(data["sampling"]),
            "interpretation": (
                "Observed quadrature differences; neither a rigorous error bound "
                "nor physical prediction uncertainty"
            ),
        },
        "assumptions": [
            "Uniform object-space radiance in all admitted directions",
            "Centered spherical/plane surfaces with explicit clear apertures",
            "Ideal lossless transmission for every non-TIR path; air at both ends",
            "Fixed declared sensor plane; no automatic focus or inferred glass dispersion",
            "Rotationally symmetric field and azimuth-independent downstream response",
        ],
        "unsupported": describe_capabilities()["unsupported"],
        "issues": []
        if converged
        else [
            {
                "code": "NUMERICAL_CHARACTERIZATION_INCONCLUSIVE",
                "message": (
                    "All requested fields need positive coarse/fine throughput, valid traces "
                    "and declared two-level agreement before a reusable payload is proposed"
                ),
            }
        ],
    }
    arrays = {key: value for key, value in fine.items() if isinstance(value, np.ndarray)}
    arrays.update(
        theta_deg=theta,
        field_radius=np.array(data["field_radius"]),
        wavelength_nm=np.array(data["wavelength_nm"]),
        coarse_irradiance_per_radiance_sr=coarse["irradiance_per_radiance_sr"],
        coarse_angular_weights=coarse["angular_weights"],
    )
    if "stop_transmission" in data:
        mask_sha = pupil_transmission.digest(data["stop_transmission"])
        summary.update(schema="lens_characterization_v2", stop_transmission_sha256=mask_sha)
        summary["request_sha256"] = pupil_transmission.digest(data)
        summary["sampling"]["power_weight"] += " * stop_power_transmittance_at_actual_intersection"
        summary["convergence"]["irradiance_integral_relative_change"] = (
            integral_relative_delta.tolist()
        )
        summary["assumptions"][2] = (
            "Passive neutral radial spectral power mask at the explicit unpowered plane stop; "
            "otherwise ideal lossless non-TIR paths in air at both ends"
        )
        summary["assumptions"].append(
            "Authored scalar stop transmission is not Fresnel/coating, polarization, "
            "bulk absorption, or general pupil-amplitude transport"
        )
        payload.update(
            field_reference_radius_mm=data["image_height_mm"][-1],
            radiometry={
                "schema": "camera_lens_radiometry_v1",
                "wavelength_nm": data["wavelength_nm"],
                "image_height_mm": data["image_height_mm"],
                "irradiance_per_radiance_sr": fine["irradiance_per_radiance_sr"].tolist(),
                "prescription_sha256": summary["request_sha256"],
                "stop_transmission_sha256": mask_sha,
                "stop_transmission": copy.deepcopy(data["stop_transmission"]),
            },
        )
    if data["schema"] in {
        "lens_prescription_v3", "lens_prescription_v4", "lens_prescription_v5",
    }:
        summary["schema"] = "lens_characterization_v" + data["schema"][-1]
        summary["assumptions"][4] = (
            "Centered prescription and radial stop permit joint field-direction rotation; "
            "on-axis annuli use the explicitly declared finite uniform-azimuth quadrature"
        )
        summary["sampling"]["joint_direction_basis"] = (
            "Actual reciprocal image-space directions with their accepted transmitted power; "
            "negate reciprocal directions for propagation into sensor; no theta/phi factorization"
        )
        summary["sampling"]["on_axis_azimuth_samples"] = data["sampling"]["on_axis_azimuth_samples"]
        summary["sampling"]["joint_direction_integration_error"] = "unknown"
        if positive and geometry_valid:
            directional = lens_direction_response.from_ray_samples(data, fine)
            payload["directional_response"] = directional
            moments = lens_direction_response.response_moments(directional)
            summary["directional_response"] = {
                "sha256": pupil_transmission.digest(directional),
                "direction_convention": directional["direction_convention"],
                "field_reference": directional["field_reference"],
                "normalization": directional["normalization"],
                "prescription_sha256": directional["prescription_sha256"],
                "stop_transmission_sha256": directional["stop_transmission_sha256"],
                **moments,
                "integration_error": "unknown",
                "physical_validation": "not_established",
            }
            arrays["directional_conditional_weights"] = np.asarray(
                directional["conditional_weights"]
            )
            arrays["on_axis_rotation_phi_deg"] = np.asarray(directional["on_axis_rotation_phi_deg"])
        else:
            summary["directional_response"] = None
    if data["schema"] in {"lens_prescription_v4", "lens_prescription_v5"}:
        summary["factory_id"] = (
            "rayoptics_reciprocal_centered_even_asphere_planar_tail_v4"
            if support["certificate"] is not None
            else "rayoptics_reciprocal_centered_even_asphere_v1"
        )
        summary["method_and_version"] = summary["factory_id"]
        summary["geometry_certificate"] = optics_surfaces.certify_prescription(data["surfaces"])
        if support["certificate"] is not None:
            summary["sampling"]["support_certificate"] = support["certificate"]
            summary["sampling"]["radial_edge_rule"] = "r_i/R=(i/N)^2"
            summary["sampling"]["radial_weight_rule"] = (
                "pi*(r_outer^2-r_inner^2)/azimuth_samples; "
                "on-axis entire-annulus area by rotational symmetry"
            )
        summary["assumptions"][1] = (
            "Centered plane/sphere/even-asphere surfaces with certified "
            "full clear-aperture geometry"
        )
        summary["unsupported"] = [x for x in summary["unsupported"] if x != "aspheres"]
        summary["unsupported"].append("nonmonotone_or_uncertified_ray_surface_intersections")
    if data["schema"] == "lens_prescription_v5":
        conjugate = copy.deepcopy(data["object_conjugate"])
        summary.update(
            factory_id="rayoptics_reciprocal_finite_planar_object_v5",
            method_and_version="rayoptics_reciprocal_finite_planar_object_v5",
            object_conjugate=conjugate,
            object_plane_coordinates={
                "reference": "first_surface_vertex_z_toward_sensor",
                "z_mm": -conjugate["distance_mm"],
                "direction_convention": "reciprocal_propagation_from_lens_toward_object",
                "invalid_samples": "zero_filled_with_object_plane_valid_false",
                "chart_mapping": "ideal_sensor_xy=reference_magnification*object_xy",
                "ray_footprint_role": "geometric_evidence_not_additional_psf_or_texture_filter",
            },
        )
        summary["assumptions"][0] = (
            "Uniform Lambertian radiance on the unbounded declared finite object plane; "
            "no object-distance inverse-square or extra cosine multiplier"
        )
        summary["sampling"].update(
            object_plane_visit_upper_bound=int(
                coarse["accepted_rays"].size + fine["accepted_rays"].size
            ),
            additional_fine_object_array_bytes=sum(
                fine[key].nbytes for key in (
                    "object_exit_position_xyz_mm", "object_plane_xy_mm",
                    "object_plane_ray_distance_mm", "object_plane_valid",
                )
            ),
        )
        payload["radiometry"].update(
            schema="camera_lens_radiometry_v2", object_conjugate=copy.deepcopy(conjugate),
        )
        if summary["directional_response"] is not None:
            summary["directional_response"]["object_conjugate"] = copy.deepcopy(conjugate)
        summary["unsupported"].extend([
            "finite_extent_or_nonuniform_object_plane_transport",
            "automatic_focus_or_inferred_magnification",
        ])
        summary["unsupported"] = [
            item for item in summary["unsupported"] if item != "finite_scene_radiance"
        ]
    return {"summary": summary, "arrays": arrays, "model_candidate": payload if converged else None}


def request_template(version=1) -> dict:
    """Authored synthetic singlet/stop fixture; no vendor or measured claim."""
    if type(version) is not int or version not in (1, 2, 3, 4, 5):
        raise ValueError("Request version must be 1, 2, 3, 4, or 5")
    wave = [450, 500, 550, 600, 650]
    data = {
        "schema": "lens_prescription_v1",
        "length_unit": "mm",
        "wavelength_unit": "nm",
        "wavelength_nm": wave,
        "surfaces": [
            {
                "id": "synthetic-stop",
                "profile": "plane",
                "radius_mm": None,
                "thickness_mm": 1.0,
                "semi_diameter_mm": 2.0,
                "refractive_index_after": [1.0] * 5,
                "is_stop": True,
            },
            {
                "id": "synthetic-front",
                "profile": "spherical",
                "radius_mm": 20.0,
                "thickness_mm": 3.0,
                "semi_diameter_mm": 4.0,
                "refractive_index_after": [1.525, 1.521, 1.518, 1.516, 1.514],
                "is_stop": False,
            },
            {
                "id": "synthetic-rear",
                "profile": "spherical",
                "radius_mm": -20.0,
                "thickness_mm": 18.0,
                "semi_diameter_mm": 4.0,
                "refractive_index_after": [1.0] * 5,
                "is_stop": False,
            },
        ],
        "field_radius": [0.0, 0.5, 1.0],
        "image_height_mm": [0.0, 1.8, 3.6],
        "illumination": {
            "kind": "uniform_object_space_radiance",
            "transmission": "ideal_lossless",
            "object_medium": "air",
            "image_medium": "air",
        },
        "sampling": {
            "polar_rings": 24,
            "azimuth_samples": 24,
            "theta_bins": 32,
            "ri_tolerance_absolute": 0.03,
            "angular_cdf_tolerance_absolute": 0.05,
        },
    }
    if version in (2, 3, 4, 5):
        data["schema"] = f"lens_prescription_v{version}"
        data["illumination"]["transmission"] = "explicit_stop_transmission"
        data["sampling"]["irradiance_tolerance_relative"] = 0.03
        data["stop_transmission"] = pupil_transmission.uniform_transmission(data)
    if version in (3, 4, 5):
        data["sampling"]["on_axis_azimuth_samples"] = 32
    if version == 4:
        data["surfaces"][1].update(
            profile="even_asphere",
            asphere={
                "schema": "camera_even_asphere_v1",
                "conic_constant": -0.5,
                "orders": [4, 6, 8],
                "coefficients": [1e-5, 0.0, 0.0],
                "coefficient_units": ["mm^-3", "mm^-5", "mm^-7"],
            },
        )
    if version == 5:
        data["object_conjugate"] = {
            **finite_conjugate.DECLARATIONS,
            "distance_mm": 500.0,
            "reference_magnification": -0.04,
        }
    return data
