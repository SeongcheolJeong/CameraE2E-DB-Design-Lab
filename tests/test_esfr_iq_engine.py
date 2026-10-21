"""Independent pixel fixtures stay synthetic; no physical camera result is claimed."""
from copy import deepcopy
import hashlib

import numpy as np
import pytest
import tifffile

from camera_platform import esfr_iq_engine as iq
from camera_platform.esfr_engine import bilinear_isp, _labels
from camera_platform.esfr_io import RawFrame
from camera_platform.registry import digest


def fixture(tmp_path, role="development", count=3, *, noise=.4, offset=0, shape=(100, 220)):
    ref = iq.nominal_reference()
    patches = [(p["id"], "color", np.array(p["linear_srgb_d65"])) for p in ref["colors"]]
    patches += [(p["id"], "gray", np.repeat(p["nominal_reflectance"], 3)) for p in ref["grays"]]
    rois = [{"id": pid, "kind": kind, "field_id": pid,
             "box": [9+(i//10)*20, 9+(i%10)*20, 9, 10]}
            for i, (pid, kind, _) in enumerate(patches)]
    mix = np.array([[.68, .12, .10], [.24, .8, .12], [.08, .08, .78]])
    black, white = np.array([64, 68, 72, 76]), np.array([4095, 4090, 4085, 4080])
    origin = [1, 1]
    yy, xx = np.indices(shape)
    sites = ((yy+origin[0]) % 2)*2 + ((xx+origin[1]) % 2)
    camera = np.full((*shape, 3), .1)
    actual = np.full((*shape, 3), .1)
    for roi, (_, _, target) in zip(rois, patches):
        y, x, h, w = roi["box"]
        camera[y:y+h, x:x+w] = target @ mix * .65 * [1.1, .8, .95]
        actual[y:y+h, x:x+w] = np.clip(target, 0, 1) ** .9
    sources = []
    for repeat in range(count):
        rng = np.random.default_rng(8100 + offset + repeat)
        mosaic = np.zeros(shape)
        for site, channel in enumerate((0, 1, 1, 2)):
            mask = sites == site
            mosaic[mask] = black[site] + camera[..., channel][mask]*(white[site]-black[site])
        mosaic = np.rint(mosaic + rng.normal(0, noise, shape)).astype("<u2")
        rgb = np.uint16(np.clip(actual + rng.normal(0, 1e-4, actual.shape), 0, 1)*65535)
        raw_path, isp_path = tmp_path / f"{role}-{repeat}.raw", tmp_path / f"{role}-{repeat}.tiff"
        mosaic.tofile(raw_path)
        tifffile.imwrite(isp_path, rgb, photometric="rgb")
        meta = {"source_kind": "synthetic", "role": role, "camera_id": "synthetic-iq-fixture",
                "session_id": role + "-session", "acquisition_id": role+f"-frame-{repeat}",
                "pixel_pitch_um": 2.9, "conditions": {"gain": 1, "exposure_s": .01},
                "raw_format": {"packing": "unpacked16", "width": shape[1], "height": shape[0],
                    "stride_bytes": shape[1]*2, "header_bytes": 0, "byte_order": "little", "bit_depth": 12,
                    "bit_shift": 0, "cfa": "RGGB", "black_level_dn": black.tolist(), "white_level_dn": white.tolist()},
                "geometry": {"crop_origin": origin, "full_sensor_shape": [shape[0]+2, shape[1]+2]},
                "isp": {"transfer": "linear", "coordinate_mapping": "same_native_raster"}}
        files = {key: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
                 for key, path in (("raw", raw_path), ("isp", isp_path))}
        record = {"id": role+f"-{repeat}", "metadata": meta, "files": files}
        record["content_hash"] = digest(record)
        sources.append(dict(record=record, raw_path=raw_path, isp_path=isp_path))
    return sources, rois


def test_reference_preserves_signed_gamut_and_hash():
    ref = iq.nominal_reference()
    assert ref["id"] == "imatest_esfr_2023_nominal_v1"
    assert len(ref["colors"]) == 18 and len(ref["grays"]) == 20
    assert min(v for p in ref["colors"] for v in p["linear_srgb_d65"]) < 0
    assert ref["sha256"] == digest({k: v for k, v in ref.items() if k != "sha256"})
    ref["colors"][0]["lab_d50"][0] = 0
    assert iq.nominal_reference()["sha256"] != digest({k: v for k, v in ref.items() if k != "sha256"})


def test_complete_development_frozen_validation_and_unknown_isp(tmp_path, monkeypatch):
    dev, rois = fixture(tmp_path)
    original = digest(dev[0]["record"])
    fitted = iq.run(dev, rois)
    profile = fitted["profile"]
    assert fitted["source_kind"] == "synthetic"
    assert digest(dev[0]["record"]) == original
    assert profile["normalization"]["black_subtraction_count"] == 1
    assert profile["normalization"]["dn_per_e_used"] is False
    assert profile["ccm"]["rank"] == 3
    assert profile["ccm"]["fitted_observations"] == 54
    assert fitted["development"]["color"]["summary"]["after"]["linear_rgb_rmse"]["mean"] < .001
    assert fitted["development"]["color"]["summary"]["after"]["linear_rgb_rmse"]["mean"] < fitted["development"]["color"]["summary"]["wb_only"]["linear_rgb_rmse"]["mean"] / 20
    row = fitted["development"]["color"]["patches"][0]
    assert np.allclose(row["wb_only_reference_rgb"], np.array(row["baseline_reference_rgb"])*profile["white_balance"]*profile["exposure_scale"])
    assert fitted["development"]["color"]["summary"]["actual_isp"]["delta_e00"]["count"] == 0
    assert fitted["development"]["color"]["summary"]["actual_isp"]["linear_rgb_rmse"]["count"] == 0
    assert row["actual_isp_rgb"] is not None
    assert row["actual_isp"]["max_abs_component_error"] is None
    val, _ = fixture(tmp_path, "validation", offset=300)
    monkeypatch.setattr(iq, "fit_profile", lambda *_: pytest.fail("validation refit"))
    result = iq.run(val, rois, frozen_profile=profile)
    assert result["profile"] == profile
    report = result["validation"]
    assert report["refit_performed"] is False and report["independent_of_training"] is True
    assert report["status"] == "diagnostic_no_acceptance_thresholds"
    assert report["same_raw_before_after"] is True
    assert len(report["color"]["patches"]) == 54
    assert report["color"]["summary"]["after"]["linear_rgb_rmse"]["mean"] < .001
    assert report["color"]["summary"]["after"]["linear_rgb_rmse"]["mean"] < report["color"]["summary"]["before"]["linear_rgb_rmse"]["mean"] / 50
    assert len(report["tone"]["patches"]) == 120
    for model in profile["tone"].values():
        assert model["input"][0] > 0 and model["input"][-1] < 1
        assert np.all(np.diff(model["output"]) >= 0)
        assert model["synthetic_endpoints_added"] is False
    assert report["tone"]["summary"]["actual_isp_luma"]["after_rmse"] < .001


def test_weighted_green_black_once_and_raw_isp_temporal_variance(tmp_path):
    sources, rois = fixture(tmp_path, noise=2)
    bundle = iq.observe_patches(sources, rois, actual_isp_color_space="srgb")
    item = bundle["captures"][0]["patches"][0]
    with RawFrame(sources[0]["raw_path"], sources[0]["record"]["metadata"]) as raw:
        g1, info1 = raw.plane(item["box"], "Gr")
        g2, info2 = raw.plane(item["box"], "Gb")
    expected = np.concatenate((g1.ravel()/(info1["white_level_dn"]-info1["black_level_dn"]),
                               g2.ravel()/(info2["white_level_dn"]-info2["black_level_dn"]))).mean()
    assert item["camera_rgb"][1] == pytest.approx(expected)
    assert item["raw"]["Gr"]["mean"] == pytest.approx(g1.mean())
    assert g1.size != g2.size
    assert len(bundle["noise"]) == 38*7
    noise = next(r for r in bundle["noise"] if r["domain"] == "raw" and r["patch_id"] == item["id"] and r["channel"] == "Gr")
    planes = []
    for source in sources:
        with RawFrame(source["raw_path"], source["record"]["metadata"]) as raw:
            planes.append(raw.plane(item["box"], "Gr")[0])
    assert noise["temporal_variance"] == pytest.approx(np.var(planes, axis=0, ddof=1).mean())
    assert all(r["frame_count"] == 3 for r in bundle["noise"])
    assert {r["domain"] for r in bundle["noise"]} == {"raw", "isp"}


@pytest.mark.parametrize("problem", ["missing_patch", "overlap", "file_changed", "wrong_role", "rank_deficient"])
def test_bad_inputs_never_silently_fit(tmp_path, problem):
    sources, rois = fixture(tmp_path)
    if problem == "missing_patch":
        rois.pop()
    elif problem == "overlap":
        rois[1]["box"] = rois[0]["box"]
    elif problem == "file_changed":
        with sources[0]["raw_path"].open("r+b") as stream:
            stream.write(b"\0\0")
    if problem in {"missing_patch", "overlap", "file_changed"}:
        with pytest.raises(ValueError):
            iq.run(sources, rois)
        return
    bundle = iq.observe_patches(sources, rois)
    if problem == "wrong_role":
        bundle["captures"][0]["role"] = "validation"
    else:
        for capture in bundle["captures"]:
            for patch in capture["patches"]:
                if patch["kind"] == "color":
                    patch["camera_rgb"] = [.2, .2, .2]
    bundle["observation_sha256"] = digest({k: v for k, v in bundle.items() if k != "observation_sha256"})
    with pytest.raises(ValueError):
        iq.fit_profile(bundle)


def test_no_clipping_and_reuse_is_rejected(tmp_path):
    dev, rois = fixture(tmp_path)
    fitted = iq.run(dev, rois)
    profile = fitted["profile"]
    sample = np.array([[-.1, .2, 1.2]])
    assert np.array_equal(iq.apply_profile_rgb(profile, sample, baseline=True), sample)
    assert np.allclose(iq.apply_profile_rgb(profile, sample), sample*profile["white_balance"]*profile["exposure_scale"] @ np.array(profile["ccm"]["matrix"]))
    changed = deepcopy(profile)
    changed["ccm"]["matrix"][0][0] += .1
    with pytest.raises(ValueError, match="hash"):
        iq.apply_profile_rgb(changed, sample)
    val, _ = fixture(tmp_path, "validation", offset=400)
    val[0]["record"]["metadata"]["session_id"] = "development-session"
    bundle = iq.observe_patches(val, rois)
    with pytest.raises(ValueError, match="session_id"):
        iq.validate_profile(profile, bundle)


def test_adc_clipped_patch_excluded_without_dropping_report_row(tmp_path):
    sources, rois = fixture(tmp_path)
    box = rois[0]["box"]
    for source in sources:
        meta = source["record"]["metadata"]
        data = np.fromfile(source["raw_path"], dtype="<u2").reshape(100, 220)
        y, x, h, w = box
        data[y:y+h, x:x+w] = 4095
        data.tofile(source["raw_path"])
        source["record"]["files"]["raw"]["sha256"] = hashlib.sha256(source["raw_path"].read_bytes()).hexdigest()
    result = iq.run(sources, rois)
    assert result["profile"]["ccm"]["fitted_observations"] == 51
    assert len(result["development"]["color"]["patches"]) == 54
    assert len(result["development"]["color"]["excluded"]) == 3
    assert all(r["reasons"] == ["raw_adc_clipping"] for r in result["development"]["color"]["excluded"])


def test_preview_applies_same_native_operator_and_repeats_are_no_fit(tmp_path):
    sources, rois = fixture(tmp_path, shape=(302, 602))
    profile = iq.run(sources, rois)["profile"]
    preview = iq.render_reference_preview(sources[0], profile, max_dimension=1024)
    meta = sources[0]["record"]["metadata"]
    with RawFrame(sources[0]["raw_path"], meta) as raw:
        levels = {ch: {"black_dn": raw.info["raw_format"]["black_level_dn"][i],
                       "white_dn": raw.info["raw_format"]["white_level_dn"][i]}
                  for i, ch in enumerate(_labels(meta["raw_format"]["cfa"]))}
        expected = bilinear_isp(raw.read(), "RGGB", meta["geometry"]["crop_origin"], levels)
    assert np.array_equal(preview["baseline_linear_rgb"], expected)
    assert np.allclose(preview["candidate_linear_rgb"], iq.apply_profile_rgb(profile, expected))
    assert preview["raw_sha256"] == sources[0]["record"]["files"]["raw"]["sha256"]
    assert preview["tone_applied"] is False and preview["same_raw_before_after"] is True
    repeated = iq.analyze_repeats(sources, rois)
    assert repeated["fit_performed"] is False and len(repeated["noise"]) == 266
    assert iq.analyze_repeats([], rois)["status"] == "unavailable_no_repeat_set"


@pytest.mark.parametrize("flag,expected", [("icc", "unapplied_icc_profile"), ("gray", "unknown_actual_isp_colorimetry")])
def test_actual_isp_colorimetry_guards_do_not_block_raw_fit(tmp_path, monkeypatch, flag, expected):
    sources, rois = fixture(tmp_path)
    decode = iq.read_isp
    def wrapped(*args, **kwargs):
        array, info = decode(*args, **kwargs)
        info["icc_profile_present" if flag == "icc" else "grayscale_replicated"] = True
        return array, info
    monkeypatch.setattr(iq, "read_isp", wrapped)
    result = iq.run(sources, rois, actual_isp_color_space="srgb")
    rows = result["development"]["color"]["patches"]
    assert all(row["actual_isp"]["delta_e00"] is None for row in rows)
    assert all(row["actual_isp"]["delta_e_status"] == expected for row in rows)
    assert all(row["actual_isp"]["linear_rgb_rmse"] is None for row in rows)
    assert all(row["actual_isp"]["max_abs_component_error"] is None for row in rows)
    assert all(row["actual_isp_rgb"] is not None for row in rows)
    assert result["profile"]["ccm"]["rank"] == 3
