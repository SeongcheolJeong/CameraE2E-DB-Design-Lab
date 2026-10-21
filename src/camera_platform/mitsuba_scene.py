"""Isolated Mitsuba adapter: fixed-wavelength path radiance, never RGB recovery.

Invoked only with validated, frozen scene inputs, in a trusted configured Python.
No film, sensor QE, aperture, PSF, RI, or pixel area integration occurs here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np


def identity():
    import drjit
    import mitsuba as mi

    mi.set_variant("scalar_spectral")
    if mi.__version__ != "3.9.1" or drjit.__version__ != "1.5.0":
        raise ValueError("Checked adapter requires Mitsuba 3.9.1 and Dr.Jit 1.5.0")
    return {
        "mitsuba": mi.__version__,
        "drjit": drjit.__version__,
        "numpy": np.__version__,
        "variant": mi.variant(),
        "python": sys.version.split()[0],
    }


def spectrum(data):
    # IrregularContinuousDistribution cannot construct a PDF for a zero spectrum.
    # The exact zero uniform spectrum preserves a perfectly black material.
    if not any(data["values"]):
        return {"type": "uniform", "value": 0.0}
    return {
        "type": "irregular",
        "wavelengths": ",".join(map(str, data["wavelength_nm"])),
        "values": ",".join(map(str, data["values"])),
    }


def load_scene(request, directory):
    import mitsuba as mi

    assets = {a["id"]: a["payload"] for a in request["assets"]}
    scene = request.get("resolved_scene", request["scene"])["payload"]
    selected = request.get("light_component_id")
    if selected is not None:
        placed = {
            *[item["area_light_id"] for item in scene["instances"]
              if item["area_light_id"]],
            *([scene["environment_light_id"]] if scene["environment_light_id"] else []),
            *[item["light_id"] for item in scene["point_lights"]],
        }
        if (not isinstance(selected, str) or not selected or selected not in placed
                or selected not in assets or assets[selected].get("kind") != "light"):
            raise ValueError("Selected Light must be an installed frozen scene emitter")
    result = {
        "type": "scene",
        "integrator": {
            "type": "path",
            "max_depth": request["max_depth"],
            "rr_depth": request["max_depth"],
        },
    }
    for i, item in enumerate(scene["instances"]):
        g, m = assets[item["geometry_id"]], assets[item["material_id"]]
        if m["bsdf"] == "diffuse":
            bsdf = {"type": "diffuse", "reflectance": spectrum(m["spectrum"])}
        else:
            bsdf = {
                "type": "roughconductor",
                "distribution": "ggx",
                "alpha": m["roughness"],
                "eta": spectrum(m["spectrum"]),
                "k": spectrum(m["extinction_k"]),
            }
        obj = {
            "type": g["primitive"],
            "bsdf": bsdf,
            "to_world": mi.ScalarTransform4f(item["to_world"]),
        }
        if g["primitive"] == "triangle_mesh":
            filename = directory / f"geometry-{i}.ply"
            lines = [
                "ply",
                "format ascii 1.0",
                f"element vertex {len(g['vertices'])}",
                "property float x",
                "property float y",
                "property float z",
                f"element face {len(g['faces'])}",
                "property list uchar int vertex_indices",
                "end_header",
            ]
            lines += [" ".join(map(str, v)) for v in g["vertices"]]
            lines += ["3 " + " ".join(map(str, f)) for f in g["faces"]]
            filename.write_text("\n".join(lines) + "\n")
            obj.update(type="ply", filename=str(filename))
        if item["area_light_id"] and (selected is None or item["area_light_id"] == selected):
            light = assets[item["area_light_id"]]
            obj["emitter"] = {"type": "area", "radiance": spectrum(light["spectrum"])}
        result[f"object_{i}"] = obj
    if scene["environment_light_id"] and (
        selected is None or scene["environment_light_id"] == selected
    ):
        light = assets[scene["environment_light_id"]]
        result["environment"] = {"type": "constant", "radiance": spectrum(light["spectrum"])}
    for i, point in enumerate(scene["point_lights"]):
        if selected is not None and point["light_id"] != selected:
            continue
        light = assets[point["light_id"]]
        result[f"point_{i}"] = {
            "type": "point",
            "position": point["position_m"],
            "intensity": spectrum(light["spectrum"]),
        }
    return mi.load_dict(result)


def render(request, directory):
    import mitsuba as mi

    backend = identity()
    scene = load_scene(request, directory)
    rows, cols = request["shape"]
    wave = request["wavelength_nm"]
    pose, geometry = request.get("resolved_pose", request["pose"]), request["sampling"]
    origin = np.asarray(pose["origin_m"])
    forward = np.asarray(pose["target_m"]) - origin
    forward /= np.linalg.norm(forward)
    right = np.cross(pose["up"], forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    pitch_mm = geometry["pixel_pitch_um"] * 1e-3
    focal = request["projection"]["focal_length_mm"]
    full, crop = geometry["full_sensor_shape"], geometry["crop_origin"]
    radiance = np.zeros((rows, cols, len(wave)), dtype=np.float64)
    stderr = np.zeros_like(radiance)
    depth = np.zeros((rows, cols), dtype=np.float64)
    hit = np.zeros((rows, cols), dtype=np.uint8)
    object_index = np.full((rows, cols), -1, dtype=np.int32)
    objects = {s.id(): int(s.id().removeprefix("object_")) for s in scene.shapes()}
    spp = request["samples_per_pixel"]
    quadrature = request.get("source_pixel_quadrature")
    retain_subcells = False
    if quadrature is not None:
        if (
            not isinstance(quadrature, dict)
            or set(quadrature) != {"model", "grid_side"}
            or quadrature["model"] not in {
                "native_sensor_area_midpoint_grid_v1",
                "native_sensor_area_midpoint_retained_subcells_v1",
            }
            or type(quadrature["grid_side"]) is not int
            or quadrature["grid_side"] not in (2, 4, 8, 16)
        ):
            raise ValueError("Unsupported native source-pixel quadrature contract")
        side = quadrature["grid_side"]
        retain_subcells = (
            quadrature["model"] == "native_sensor_area_midpoint_retained_subcells_v1"
        )
        if rows * cols * len(wave) * spp * side**2 > 262144:
            raise ValueError("Source-pixel quadrature exceeds the primary-path budget")
    subcell_radiance = (
        np.zeros((rows, cols, side, side, len(wave)), dtype=np.float64)
        if retain_subcells else None
    )
    subcell_error = np.zeros_like(subcell_radiance) if retain_subcells else None
    sampler = mi.load_dict({"type": "independent", "sample_count": spp})
    sampler.seed(request["seed"])
    for y in range(rows):
        for x in range(cols):
            dx = (crop[1] + x + 0.5 - full[1] / 2) * pitch_mm
            dy = (full[0] / 2 - crop[0] - y - 0.5) * pitch_mm
            direction = forward * focal + right * dx + up * dy
            direction /= np.linalg.norm(direction)
            ray = mi.Ray3f(mi.Point3f(origin), mi.Vector3f(direction))
            si = scene.ray_intersect(ray)
            if si.is_valid():
                hit[y, x] = 1
                depth[y, x] = si.t
                object_index[y, x] = objects[si.shape.id()]
            if quadrature is not None:
                # Integrate scene radiance over the native source cell only.
                # The downstream pixel-integrated PSF still receives one value
                # at this cell's center; no fractional-offset PSF is inferred.
                weight = 1.0 / side**2
                for sub_y in range(side):
                    for sub_x in range(side):
                        sub_dx = (
                            crop[1] + x + (sub_x + 0.5) / side - full[1] / 2
                        ) * pitch_mm
                        sub_dy = (
                            full[0] / 2 - crop[0] - y - (sub_y + 0.5) / side
                        ) * pitch_mm
                        sub_direction = forward * focal + right * sub_dx + up * sub_dy
                        sub_direction /= np.linalg.norm(sub_direction)
                        sub_ray = mi.Ray3f(mi.Point3f(origin), mi.Vector3f(sub_direction))
                        for k, wavelength in enumerate(wave):
                            sub_ray.wavelengths = mi.Spectrum(wavelength)
                            mean, m2 = 0.0, 0.0
                            for n in range(1, spp + 1):
                                value = float(
                                    scene.integrator().sample(scene, sampler, sub_ray)[0][0]
                                )
                                if not np.isfinite(value) or value < 0:
                                    raise ValueError(
                                        "Renderer returned nonfinite or negative radiance"
                                    )
                                delta = value - mean
                                mean += delta / n
                                m2 += delta * (value - mean)
                                sampler.advance()
                            sample_error = np.sqrt(max(0.0, m2) / (spp - 1) / spp)
                            if retain_subcells:
                                subcell_radiance[y, x, sub_y, sub_x, k] = mean
                                subcell_error[y, x, sub_y, sub_x, k] = sample_error
                            radiance[y, x, k] += weight * mean
                            stderr[y, x, k] += weight**2 * sample_error**2
                stderr[y, x] = np.sqrt(stderr[y, x])
                continue
            for k, wavelength in enumerate(wave):
                # All four packet lanes carry the same physical wavelength.
                # Integrator.sample estimates L(lambda); no wavelength PDF or CIE weight.
                ray.wavelengths = mi.Spectrum(wavelength)
                mean, m2 = 0.0, 0.0
                for n in range(1, spp + 1):
                    value = float(scene.integrator().sample(scene, sampler, ray)[0][0])
                    if not np.isfinite(value) or value < 0:
                        raise ValueError("Renderer returned nonfinite or negative radiance")
                    delta = value - mean
                    mean += delta / n
                    m2 += delta * (value - mean)
                    sampler.advance()
                radiance[y, x, k] = mean
                stderr[y, x, k] = np.sqrt(max(0.0, m2) / (spp - 1) / spp)
    for name, data in {
        "spectral-radiance": radiance,
        "radiance-mc-standard-error": stderr,
        "first-hit-depth-m": depth,
        "first-hit-valid": hit,
        "first-hit-object-index": object_index,
    }.items():
        np.save(directory / (name + ".npy"), data, allow_pickle=False)
    if retain_subcells:
        np.save(directory / "source-subcell-radiance.npy", subcell_radiance, allow_pickle=False)
        np.save(directory / "source-subcell-mc-standard-error.npy", subcell_error,
                allow_pickle=False)
    summary = {
        "schema": "fixed_wavelength_path_radiance_v1",
        "backend": backend,
        "unit": "W/m^2/sr/nm",
        "wavelength_nm": wave,
        "shape": list(radiance.shape),
        "sampling": "pixel_centre_pinhole_fixed_wavelength_path_samples",
        "samples_per_pixel": spp,
        "max_depth": request["max_depth"],
        "seed": request["seed"],
        "mc_standard_error": "sample_estimate_only_not_error_bound_or_total_uncertainty",
        "depth": "first surface ray distance in metres; zero with invalid mask for misses",
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
    }
    if quadrature is not None:
        summary["sampling"] = "native_sensor_area_midpoint_grid_v1_fixed_wavelength_path_samples"
        summary["source_pixel_quadrature"] = {
            **quadrature,
            "integrator_path_samples": rows * cols * len(wave) * spp * side**2,
            "stochastic_paths_per_subcell_wavelength": spp,
            "sampler_seed": request["seed"],
            "sampler_order": "crop_row_column_subcell_row_column_wavelength_path",
            "subcell_centres_yx": [
                [(sub_y + 0.5) / side, (sub_x + 0.5) / side]
                for sub_y in range(side) for sub_x in range(side)
            ],
            "subcell_weight": 1.0 / side**2,
            "subcell_coordinates": "native_sensor_pixel_yx_from_top_left",
            "projection_geometry_interpretation": (
                "pinhole_pixel_centre_reference_with_subcell_offsets"
            ),
            "source_cell_representation": (
                "area_mean_with_retained_subcell_radiance"
                if retain_subcells else "area_mean_radiance_assigned_to_native_cell_centre"
            ),
            "first_hit_auxiliary": "pixel_centre_ray_only",
            "spatial_quadrature_error": "unknown",
            "continuous_scene_psf_integration": False,
            "receiver_aperture_filter_applied_here": False,
            "physical_validation": "not_established",
        }
        if retain_subcells:
            summary["source_pixel_quadrature"].update(
                retained_radiance_artifact="source-subcell-radiance.npy",
                retained_standard_error_artifact="source-subcell-mc-standard-error.npy",
                fractional_source_psf_model="bilinear_receiver_integrated_impulse_v1",
            )
    if "scene_motion_evidence" in request:
        summary["scene_motion"] = request["scene_motion_evidence"]
    if request.get("light_component_id") is not None:
        light = next(a for a in request["assets"]
                     if a["id"] == request["light_component_id"])
        summary["light_component"] = {
            "model": "one_frozen_scene_light_asset_v1",
            "light_asset_id": light["id"],
            "light_asset_sha256": light["content_hash"],
            "whole_scene_illumination_id": request["scene"]["illumination_id"],
            "other_emitters": "disabled_for_this_render_geometry_retained",
            "superposition": "linear_scene_radiance_expectation_not_finite_sample_identity",
        }
    (directory / "renderer-summary.json").write_text(json.dumps(summary, allow_nan=False, indent=2))
    return summary


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(identity()))
    else:
        render(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]))
