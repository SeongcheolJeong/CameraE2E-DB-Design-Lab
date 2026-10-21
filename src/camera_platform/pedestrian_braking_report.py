"""Sequence viewer: exact simulated frames and event-level braking evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .road_design_inputs import write_json


def build_report(package, spec, decision, records, resources):
    sequence_views = []
    for event in spec["events"]:
        for candidate in spec["candidates"]:
            path = package / "calculations" / event["id"] / candidate["id"] / "sequence.json"
            if not path.exists():
                continue
            seq = json.loads(path.read_text())
            inference = json.loads((path.parent / f"inference-{spec['seeds'][0]}.json").read_text())
            sequence_views.append(
                {
                    "event_id": event["id"],
                    "risk": event["risk"],
                    "split": event["split"],
                    "candidate_id": candidate["id"],
                    "records": seq["records"],
                    "frames": [
                        {
                            **frame,
                            "image": seq["frame_directories"][frame["token"]]
                            + f"/rgb-{spec['seeds'][0]}.png",
                        }
                        for frame in inference["frames"]
                    ],
                    "motion": event["motion"],
                }
            )
    write_json(
        package / "viewer.json",
        {
            "title": spec["title"],
            "decision": decision,
            "records": records,
            "resources": resources,
            "braking": spec["braking"],
            "timing": spec["timing"],
            "sequences": sequence_views,
        },
    )
    static = Path(__file__).with_name("static")
    for source, target in (
        ("pedestrian-braking-report.html", "report.html"),
        ("pedestrian-braking-report.js", "pedestrian-braking-report.js"),
    ):
        shutil.copyfile(static / source, package / target)
    (package / "README.md").write_text(
        f"# {spec['title']}\n\n결론: **{decision['status']} / {decision['recommended_id']}**\n\n"
        "원본 RAW와 고정 ISP, 프레임 timestamp·추적·정답 연관·선정 lock을 보존했습니다.\n\n"
        "평가 대상은 가상 센서 extent의 RGB-derived spectral proxy이며, "
        "물리 카메라·AEB 인증·폐루프 충돌 회피 결과가 아닙니다. "
        "입력 이미지의 기존 blur/noise/JPEG/clipping은 남아 있습니다. "
        "더 큰 센서 면적·픽셀 수의 비용도 포함합니다. "
        "인지 지연 100ms, 잔여 제동지연 0.3s, 일정 감속 6m/s², buffer 2m는 시험 가정입니다. "
        "원본 2Hz 주석과 보간 정답을 구분하고 불확실한 사건을 분모에서 제외하지 않습니다.\n\n"
        "nuScenes/Motional: 비상업 내부 연구 범위. 상업 권한은 확인되지 않았습니다.\n\n"
        "검증: `PYTHONPATH=src .venv-platform/bin/python -B "
        "tools/run_pedestrian_braking_study.py verify --package <package>`\n"
        "재실행: 같은 명세와 입력을 새 package에 복사하는 "
        "`replay --destination <new path>`를 사용합니다.\n",
        encoding="utf-8",
    )
