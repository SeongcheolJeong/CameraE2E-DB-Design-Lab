#!/usr/bin/env python3
"""Verify saved native wide-angle delivery artifacts; never infer success from previews."""

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from camera_platform.virtual_lab_job import sha256


def verify(record, state):
    assert record["status"] == "succeeded", record.get("error")
    root = state / "virtual-lab" / record["id"]
    result = json.loads((root / "result.json").read_text())
    manifest = json.loads((root / "manifest.json").read_text())
    assert sha256(root / "manifest.json") == record["result"]["summary"]["manifest_sha256"]
    for relative, expected in manifest["files"].items():
        assert sha256(root / relative) == expected, relative
    capture = json.loads((root / "capture.json").read_text())
    assert capture["geometry"]["native_shape"] == [1080, 1920]
    assert capture["geometry"]["horizontal_fov_deg"] == 120
    for kind, entry in capture["captures"].items():
        raw = np.load(root / entry["raw"]["path"], mmap_mode="r", allow_pickle=False)
        mean = np.load(root / entry["mean"]["path"], mmap_mode="r", allow_pickle=False)
        assert raw.shape == mean.shape == (1080, 1920)
        assert raw.dtype == np.uint16
        assert np.isfinite(mean).all() and mean.min() >= 0
        native = result["native_isp_artifacts"][kind]
        for key in ("linear", "processed"):
            array = np.load(root / native[key]["path"], mmap_mode="r", allow_pickle=False)
            assert array.shape == (1080, 1920, 3) and np.isfinite(array).all()
        with Image.open(root / native["display"]["path"]) as image:
            assert image.size == (1920, 1080)
        with Image.open(root / result["image_sets"][kind]["isp"]) as image:
            assert image.size == (640, 360)
    resolution = result["tests"].get("resolution")
    valid_edges = 0
    if resolution:
        assert len(resolution["sites"]) == 9
        for site in resolution["sites"].values():
            for direction in ("radial", "tangential"):
                valid_edges += site[direction]["linear_system"]["status"] == "ok"
        assert valid_edges == 18, f"Only {valid_edges}/18 supported edge measurements"
    lab_metrics = {}
    for kind in capture["requested_tests"]:
        assert kind in result["tests"], f"Missing requested Lab test: {kind}"
    if "color" in result["tests"]:
        color = result["tests"]["color"]
        assert color["status"] == "ok" and len(color["patches"]) == 24
        lab_metrics["color_delta_e00_mean"] = color["delta_e00_mean"]
    if "noise" in result["tests"]:
        noise = result["tests"]["noise"]
        assert noise["status"] == "ok"
        assert noise["repeat_count"] == record["specification"]["frames"]
        assert len(noise["photon_transfer_points"]) == 5
        assert all(
            point["status"] == "ok" and point["repeat_count"] == noise["repeat_count"]
            for point in noise["photon_transfer_points"]
        )
        lab_metrics.update(
            temporal_noise_repeat_count=noise["repeat_count"],
            photon_transfer_levels=5,
            gr_temporal_noise_dn=noise["temporal_std"],
            gr_snr_db=noise["snr_db"],
        )
    source = result.get("source_run_id")
    if source:
        original = state / "virtual-lab" / source
        for folder in ("raw", "arrays"):
            for path in (original / folder).iterdir():
                assert sha256(path) == sha256(root / folder / path.name)
        for path in (original / "isp").glob("*-processed.npy"):
            if sha256(path) != sha256(root / "isp" / path.name):
                break
        else:
            raise AssertionError("ISP replay did not change any native processed output")
    return {
        "id": record["id"],
        "sensor": record["specification"]["sensor_id"],
        "projection": capture["geometry"]["projection"],
        "scene": record["specification"]["scene"],
        "native_shape": [1080, 1920],
        "verified_files": len(manifest["files"]),
        "verified_bytes": sum((root / relative).stat().st_size for relative in manifest["files"]),
        "valid_directional_sfr": valid_edges,
        "lab_metrics": lab_metrics,
        "sampling": result["tests"].get("sampling"),
        "resources": result.get("resources"),
        "timings": result["timings"],
        "source_run_id": source,
        "source_code": record["source_code"],
        "catalog_sha256": record["catalog_sha256"],
        "report_url": record["result"]["summary"]["report_url"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    records = [record for path in args.runs for record in json.loads(Path(path).read_text())]
    results = [verify(record, Path(args.state_dir)) for record in records]
    acquisitions = [item for item in results if item["source_run_id"] is None]
    assert {(item["sensor"], item["projection"]) for item in acquisitions} >= {
        (sensor, projection)
        for sensor in ("S1", "S2")
        for projection in ("rectilinear", "equidistant")
    }
    output = {"schema": "camera_virtual_lab_wide_delivery_v1", "verified": True, "runs": results}
    Path(args.output).write_text(json.dumps(output, indent=2))
    print(json.dumps({"verified_runs": len(results), "output": args.output}))


if __name__ == "__main__":
    main()
