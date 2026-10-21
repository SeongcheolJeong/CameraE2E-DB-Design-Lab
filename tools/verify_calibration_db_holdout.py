"""Verify exported observations, patch errors and ISP algebra from saved RAW bytes."""

import argparse
import hashlib
import json
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
from camera_platform.calibration_db_holdout import reference_raw

from pyisetcam.bayer_demosaic import demosaic_bayer

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path)
parser.add_argument("--output", type=Path)
args = parser.parse_args()
root = args.root.resolve()
result = json.loads((root / "report.json").read_text())
manifest = json.loads((root / "manifest.json").read_text())["files"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


assert all(sha((root / name).read_bytes()) == value for name, value in manifest.items())
with zipfile.ZipFile(root / "evidence.zip") as archive:
    assert all(sha(archive.read(name)) == value for name, value in manifest.items())
readout = result["protocol"]["readout"]
profile = result["protocol"]["configuration"]["isp"]
assert profile["raw_denoise"]["strength"] == profile["linear_sharpen"]["amount"] == 0
assert not profile["lsc_enabled"]
all_hashes = []
pixel_error = 0.0
seed_replays = 0
for field in result["fields"]:
    independent = {}
    for role in ("before", "observation", "after", "replicate"):
        saved = field["series"][role]
        stack, linears = [], []
        for item in saved["raw_frames"]:
            data = (root / item["path"]).read_bytes()
            assert sha(data) == item["sha256"]
            all_hashes.append(item["sha256"])
            raw = np.load(root / item["path"])
            stack.append(raw)
            # Keep the pinned adapter's arithmetic order: adaptive demosaic
            # uses gradient ties, so algebraically equivalent reordering differs.
            norm = 4095 - readout["black_level_dn"]
            normalized = (raw.astype(float) - readout["black_level_dn"]) / norm
            electrons = demosaic_bayer(
                normalized * (norm / readout["dn_per_e"]),
                profile["demosaic_method"],
                cfa_pattern="RGGB",
            )
            linears.append(electrons @ np.asarray(saved["isp_matrix"]))
            if role in ("observation", "replicate"):
                rate = np.load(root / field["field_id"] / role / "mean-electron-rate.npy")
                expected = reference_raw(
                    rate,
                    readout,
                    result["protocol"]["configuration"]["exposure_ms"] / 1000,
                    item["seed"],
                )
                np.testing.assert_array_equal(raw, expected)
                seed_replays += 1
        mean, linear = np.mean(stack, axis=0), np.mean(linears, axis=0)
        original = np.load(root / field["field_id"] / role / "mean-linear.npy")
        pixel_error = max(pixel_error, float(np.max(np.abs(linear - original))))
        np.testing.assert_allclose(linear, original, atol=1e-12, rtol=0)
        raw_patches, isp_patches = [], []
        yy, xx = np.indices(mean.shape)
        sites = [(yy % 2 == 0) & (xx % 2 == 0), yy % 2 != xx % 2, (yy % 2 == 1) & (xx % 2 == 1)]
        for patch in range(24):
            row, col = divmod(patch, 6)
            mask = (
                (yy >= (row + 0.25) * 256 / 4)
                & (yy < (row + 0.75) * 256 / 4)
                & (xx >= (col + 0.25) * 256 / 6)
                & (xx < (col + 0.75) * 256 / 6)
            )
            raw_patches.append([float(mean[mask & site].mean()) for site in sites])
            isp_patches.append(linear[mask].mean(axis=0))
        np.testing.assert_allclose(raw_patches, saved["raw_patch_rgb_dn"], atol=1e-12, rtol=0)
        np.testing.assert_allclose(isp_patches, saved["isp_patch_rgb"], atol=1e-12, rtol=0)
        independent[role] = {"raw": raw_patches, "isp": isp_patches}
    for domain in ("raw", "isp"):
        for role in ("before", "after"):
            difference = np.asarray(independent[role][domain]) - np.asarray(
                independent["observation"][domain]
            )
            rmse = float(np.sqrt(np.mean(difference**2)))
            assert abs(rmse - field["metrics"][domain][role]["rmse"]) < 1e-12
    assert field["series"]["qe_only"]["raw_frames"] == field["series"]["after"]["raw_frames"]
assert len(all_hashes) == len(set(all_hashes)) == 64


class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images, self.external = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        source = attrs.get("src", attrs.get("href", ""))
        if tag == "img":
            self.images.append(source)
        if tag in ("img", "script", "link") and source.startswith(("http:", "https:")):
            self.external.append(source)


parser = Parser()
parser.feed((root / "report.html").read_text())
assert len(parser.images) == 16 and not parser.external
assert all(src.startswith("data:image/png;base64,") for src in parser.images)
verification = {
    "status": "passed",
    "manifest_files": len(manifest),
    "unique_raw_frames": 64,
    "reference_seed_replays": seed_replays,
    "isp_pixel_recomputations": 64,
    "maximum_isp_array_error": pixel_error,
    "manual_patch_observer_matches": True,
    "signed_rmse_recomputed": True,
    "html_inline_images": 16,
    "external_resources": 0,
}
if args.output:
    args.output.write_text(json.dumps(verification, indent=2))
print(json.dumps(verification))
