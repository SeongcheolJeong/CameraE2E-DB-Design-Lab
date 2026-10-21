"""Compare common response observations while preserving generated control outcomes."""

from copy import deepcopy

from .control_observation import METHOD, metric_fields, validate_test


def compare_response(prediction, measurement, test):
    from .validation import _compare_metric, _issue, check_domain

    spec = validate_test(test)
    records = [deepcopy(prediction), deepcopy(measurement)]
    observations = [r.get("response_observation") for r in records]
    if any(
        not isinstance(o, dict) or o.get("method") != test["observation_method"]
        for o in observations
    ):
        raise ValueError("Response comparison requires both frozen RAW/control-log analyses")
    points = [o["points"] for o in observations]
    ids = [[p["frame_id"] for p in row] for row in points]
    expected = (
        [f["frame_id"] for f in test["stimulus"]["planned_frames"]]
        if spec.reduction == "trajectory"
        else ["window"]
    )
    if any(row != expected for row in ids):
        raise ValueError(
            "Response observations must preserve the complete ordered test coordinates"
        )
    for record, observation in zip(records, observations, strict=True):
        fields = metric_fields(observation)
        if any(
            record.get(k) != fields[k]
            for k in ("metric_id", "unit", "values", "axes", "clock", "observation_method")
        ):
            raise ValueError("Response values or coordinates differ from their preserved analysis")

    paired = [i for i in range(len(expected)) if all(row[i]["value"] is not None for row in points)]
    # Project only explicitly paired coordinates into the scalar/vector comparator.
    # Timing, native geometry, policy and every actual applied domain are checked below.
    # Keep full original observations in the report; missing data never becomes zero.
    projected_test = deepcopy(test)
    projected_test.update(
        clock=None, frame_conditions=None, stimulus={"schema": "paired_control_response_metric_v1"}
    )
    for record, observation in zip(records, observations, strict=True):
        nonmissing = [i for i, p in enumerate(observation["points"]) if p["value"] is not None]
        record["values"] = [observation["points"][i]["value"] for i in paired] if paired else None
        record["axes"] = (
            [{"name": "response_sample", "unit": "1", "values": [expected[i] for i in paired]}]
            if paired
            else []
        )
        uncertainty = record.get("uncertainty", {})
        if "values" in uncertainty:
            if len(uncertainty["values"]) != len(nonmissing):
                raise ValueError(
                    "Response bounds must match the observed, nonmissing metric coordinates"
                )
            uncertainty["values"] = [uncertainty["values"][nonmissing.index(i)] for i in paired]
        record["clock"] = None
        record["frame_conditions"] = None
        record.pop("frame_valid_domains", None)
        record.pop("acquisition_mode", None)
        if isinstance(record.get("valid_domain"), dict):
            record["valid_domain"] = {
                k: v for k, v in record["valid_domain"].items() if k not in {"exposure_s", "gain"}
            }
    report = _compare_metric(*records, projected_test)
    issues = report["issues"]
    missing = []
    for i in range(len(expected)):
        if i in paired:
            continue
        statuses = [row[i]["status"] for row in points]
        if statuses == ["no_issued_command", "no_issued_command"]:
            continue  # No latency exists at this source frame on either side.
        missing.append(
            {"sample_id": expected[i], "prediction": statuses[0], "measurement": statuses[1]}
        )
    if missing:
        issues.append(
            _issue(
                "incomplete_response_coverage",
                "Some required response samples are unknown.",
                "response_observation.points",
            )
        )
    for name, original, observation in zip(
        ("prediction", "measurement"), (prediction, measurement), observations, strict=True
    ):
        if observation.get("clock") != test["clock"]:
            issues.append(
                _issue("response_clock_mismatch", "Test-relative clocks differ.", name, "error")
            )
        for frame in observation["alignment"]:
            if frame["status"] != "aligned":
                issues.append(
                    _issue(
                        "response_time_alignment",
                        "Frame alignment is " + frame["status"],
                        name + "." + frame["frame_id"],
                        "error" if frame["status"] == "mismatch" else "unknown",
                    )
                )
        for frame in observation["acquisition"]["frames"]:
            applied = frame["applied"]
            if applied is None:
                issues.append(
                    _issue(
                        "unknown_applied_controls",
                        "Actual settings are not known.",
                        name + "." + frame["frame_id"],
                    )
                )
                continue
            conditions = {
                **original["conditions"],
                "exposure_s": {"value": applied["exposure_ns"] / 1e9, "unit": "s"},
                "gain": {"value": applied["gain"], "unit": "1"},
            }
            issues.extend(
                check_domain(
                    conditions,
                    prediction.get("valid_domain"),
                    path=name + ".applied_domain." + frame["frame_id"],
                )
            )
    report["response_comparison"] = {
        "operator": METHOD,
        "specification": spec.model_dump(),
        "paired_samples": [expected[i] for i in paired],
        "missing_samples": missing,
        "prediction": observations[0],
        "measurement": observations[1],
        "control_policy": "Compare actual applied settings as outcomes; do not require equality.",
        "latency_coordinates": "Command source frame; both events use the same logged clock.",
        "uncertainty_scope": "Bounds must cover the selected reduced metric, including timing, "
        "sampling and target classification where applicable.",
    }
    severities = {issue["severity"] for issue in issues}
    if "error" in severities:
        report["status"] = "INVALID_COMPARISON"
    elif "out_of_domain" in severities:
        report["status"] = "OUT_OF_DOMAIN"
    elif "unknown" in severities:
        report["status"] = "INCONCLUSIVE"
    report["comparison_valid"] = not bool(severities & {"error", "unknown", "out_of_domain"})
    report["physical_validation_pass"] = (
        test["validation_kind"] == "physical_validation" and report["status"] == "PASS"
    )
    if not report["physical_validation_pass"]:
        report["claim"] = "This report does not establish an independent physical validation pass."
    return report
