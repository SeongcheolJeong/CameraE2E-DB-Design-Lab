"""Read-only comparison with the retained earlier completed calibration."""
import json

from .registry import IntegrityError, digest


def comparison_history(service, job, current):
    from . import calibration_completion, calibration_lab

    reports = [r for r in service.registry.list(calibration_completion.COLLECTION)
               if r["campaign_id"] == job["campaign_snapshot"]["id"]
               and r["study_id"] != job["parent_study_id"]]
    for report in sorted(reports, key=lambda r: (r["created_at"], r["id"]), reverse=True):
        prior = calibration_lab.get(service, report["validation_id"])
        if digest(prior["result"]) != report["validation_result_sha256"]:
            raise IntegrityError("Historical completion validation result changed")
        evidence = prior["result"]["summary"].get("esfr_image_validation")
        if not evidence:
            continue
        failures = [c for c in evidence["validation"]["checks"] if c["status"] == "fail"]
        if not failures:
            continue
        worst = max(failures, key=lambda c: c["value"] - c["threshold"])
        focus = worst["roi_id"]
        sha = prior["result"]["summary"]["files"]["esfr-images/result.json"]
        binding = service.registry.artifact_binding(sha, owner_kind="runs", owner_id=prior["id"])
        previous = json.loads(binding["path"].read_text())
        rows = []
        for result, method, candidate in ((previous, "analytic_curve", prior["candidate_sha256"]),
                                           (current, "development_native_bayer_same_observer_gaussian_v1", job["candidate_sha256"])):
            for c in result["comparisons"]:
                if c["roi_id"] != focus:
                    continue
                threshold = result["validation"]["thresholds"]
                raw, isp = c["raw"].get("after_rmse"), c["isp"].get("after_rmse")
                rows.append(dict(
                    dataset_role="development" if c["role"] == "development" else
                    "previous_holdout" if result is previous else "new_holdout",
                    fit_method=method, candidate_sha256=candidate,
                    capture_id=c["capture_id"], roi_id=focus, raw_rmse=raw, isp_rmse=isp,
                    raw_threshold=threshold.get("raw_sfr_rmse"), isp_threshold=threshold.get("isp_sfr_rmse"),
                    status="pass" if raw is not None and isp is not None
                    and raw <= threshold.get("raw_sfr_rmse", -1)
                    and isp <= threshold.get("isp_sfr_rmse", -1) else "fail",
                ))
        return dict(
            prior_report_url=report["report_url"], prior_validation_id=prior["id"],
            prior_candidate_sha256=prior["candidate_sha256"], focus_roi_id=focus, rows=rows,
            thresholds_unchanged=evidence["validation"]["thresholds"] == current["validation"]["thresholds"],
            scope="Historical holdout is retained for diagnosis context; it is not used by the new fitter.",
        )
    return None
