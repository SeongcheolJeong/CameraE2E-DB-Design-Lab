#!/usr/bin/env python3
"""Submit or resume the pinned DB design demo through the common SDK."""

import argparse
import json
from pathlib import Path

from camera_platform.sdk import PlatformClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--run", help="Resume an existing job without creating another capture")
    parser.add_argument("--sensor", default="imx662", choices=["imx662", "imx307"])
    parser.add_argument(
        "--comparison",
        default="lens_sensor",
        choices=["lens_sensor", "aperture_diffraction", "aperture_sweep", "aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"],
    )
    parser.add_argument(
        "--f-numbers", type=float, nargs="+", help="Ascending apertures for a new sweep, or saved subset for shutter_motion"
    )
    parser.add_argument("--min-snr-db", type=float, help="Aperture selection minimum temporal SNR")
    parser.add_argument(
        "--min-relative-signal", type=float, help="Aperture selection minimum relative signal"
    )
    parser.add_argument(
        "--max-saturation-fraction",
        type=float,
        help="Aperture selection maximum saturation fraction",
    )
    parser.add_argument("--name", help="Name for a newly submitted comparison")
    parser.add_argument("--source-run", help="Completed aperture sweep source for condition studies")
    parser.add_argument("--luminance-cd-m2", type=float, nargs="+", help="Scene luminance, not lux")
    parser.add_argument("--exposure-ms", type=float, nargs="+", help="Exposure duration candidates")
    parser.add_argument("--line-time-us", type=float, nargs="+", help="Declared per-row sensor times; 0 is matched global shutter")
    parser.add_argument("--operating-mode", choices=['kurokesu_1080p30_raw10','assumed_1080p60','assumed_1080p120'])
    parser.add_argument("--fps", type=float, help="Fixed requested cadence within the selected mode limits")
    parser.add_argument("--located-source-run", help="Reuse preserved located PSFs and pre-noise rates")
    parser.add_argument("--readout-ms", type=float, nargs="+", help="Virtual 256-row first-to-last scan; 0 is global shutter")
    parser.add_argument("--object-speed-m-s", type=float, nargs="+", help="Transverse target speeds")
    parser.add_argument("--object-distance-m", type=float, help="Declared target distance for speed projection")
    parser.add_argument("--output", type=Path, default=Path("outputs/db-design-delivery-20261012"))
    args = parser.parse_args()
    policy = {
        key: value
        for key, value in {
            "min_snr_db": args.min_snr_db,
            "min_relative_signal": args.min_relative_signal,
            "max_saturation_fraction": args.max_saturation_fraction,
        }.items()
        if value is not None
    }
    if args.f_numbers is not None and args.comparison not in {"aperture_sweep", "shutter_motion", "sensor_shutter"}:
        parser.error("Aperture list and selection thresholds require --comparison aperture_sweep")
    if policy and args.comparison not in {"aperture_sweep", "aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"}:
        parser.error("Selection thresholds require an aperture comparison")
    if args.comparison in {"aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"} and not (args.source_run or args.run):
        parser.error("--source-run is required for a new condition study")
    if args.comparison not in {"aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"} and any(
        v is not None for v in (args.source_run, args.luminance_cd_m2, args.exposure_ms)
    ):
        parser.error("Source run and luminance/exposure axes require a condition or motion study")
    if args.comparison not in {"aperture_motion", "shutter_motion", "sensor_shutter"} and any(
        v is not None for v in (args.object_speed_m_s, args.object_distance_m)
    ):
        parser.error("Object speed/distance require aperture_motion")
    if args.readout_ms is not None and args.comparison != "shutter_motion":
        parser.error("Readout scan requires shutter_motion")
    if args.line_time_us is not None and args.comparison != "sensor_shutter":
        parser.error("Line time requires sensor_shutter")
    if any(v is not None for v in (args.operating_mode,args.fps,args.located_source_run)) and args.comparison != 'sensor_shutter':
        parser.error('Operating mode/FPS/located source require sensor_shutter')
    if args.operating_mode and args.line_time_us:
        parser.error('Operating mode fixes line time; do not also pass --line-time-us')
    args.output.mkdir(parents=True, exist_ok=True)
    with PlatformClient(args.base_url, timeout=120) as client:
        if args.run:
            identity = args.run
        else:
            options = client.db_design_options()
            spec = (
                options["recipes"][args.comparison]
                if args.comparison in {"aperture_diffraction", "aperture_sweep", "aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"}
                else {**options["defaults"], "sensor_keys": ["reference_imx462", args.sensor]}
            )
            if args.comparison == "aperture_sweep":
                if args.f_numbers is not None:
                    spec["f_numbers"] = args.f_numbers
                    spec.pop("lens_keys")  # Server binds canonical keys from the numeric list.
                spec["selection_policy"] = {**spec["selection_policy"], **policy}
            if args.comparison in {"aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"}:
                spec["source_run_id"] = args.source_run
                if args.luminance_cd_m2 is not None:
                    spec["luminance_cd_m2"] = args.luminance_cd_m2
                if args.exposure_ms is not None:
                    spec["exposure_ms"] = args.exposure_ms
                spec["selection_policy"] = {**spec["selection_policy"], **policy}
            if args.comparison in {"aperture_motion", "shutter_motion", "sensor_shutter"}:
                if args.object_speed_m_s is not None:
                    spec["object_speed_m_s"] = args.object_speed_m_s
                if args.object_distance_m is not None:
                    spec["object_distance_m"] = args.object_distance_m
            if args.comparison == "sensor_shutter":
                if args.operating_mode:
                    spec.pop('line_time_us',None)
                    spec['operating_mode_id']=args.operating_mode
                for key,value in [('fps',args.fps),('located_source_run_id',args.located_source_run)]:
                    if value is not None: spec[key]=value
                if args.line_time_us is not None:
                    spec["line_time_us"] = args.line_time_us
                if args.f_numbers is not None:
                    spec["f_numbers"] = args.f_numbers
            if args.comparison == "shutter_motion":
                if args.readout_ms is not None:
                    spec["readout_ms"] = args.readout_ms
                if args.f_numbers is not None:
                    spec["f_numbers"] = args.f_numbers
            if args.name:
                spec["name"] = args.name
            job = client.submit_db_design_study(spec)
            identity = job["id"]
            (args.output / "submitted.json").write_text(json.dumps(job, indent=2))
        print(identity, flush=True)
        result = client.wait(
            identity,
            timeout=14500 if args.comparison in {"aperture_sweep", "aperture_conditions", "aperture_motion", "shutter_motion", "sensor_shutter"} else 3700,
            poll_interval=10,
        )
        (args.output / "delivery.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] != "succeeded":
            raise RuntimeError(f"{identity}: {result['status']}: {result.get('error')}")
        summary = result["result"]["summary"]
        print(args.base_url + summary["result_url"], flush=True)
        print(f"Native RAW: {summary['capture_count']}; package: {summary['package_bytes']} bytes")


if __name__ == "__main__":
    main()
