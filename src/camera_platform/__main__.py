"""python -m camera_platform serve|worker|demo|verify|export-openapi|backup|restore|init-r1|seed-r1-modes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "serve", "worker", "demo", "verify", "export-openapi", "backup", "restore",
            "init-r1", "seed-r1-modes",
        ],
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument(
        "--r1-project-id",
        help="Create or reopen a separate R1 engineering project state",
    )
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--external-worker", action="store_true")
    parser.add_argument(
        "--empty", action="store_true", help="Start without synthetic engineering fixtures"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--expected-project-id",
        help="Require this R1 project identity when backing up or restoring state",
    )
    args = parser.parse_args()
    state = args.state_dir or args.workspace / "outputs/camera-platform"
    if args.expected_project_id is not None and args.command not in {"backup", "restore"}:
        parser.error("--expected-project-id is for backup or restore")
    if args.r1_project_id is not None or args.command in {"init-r1", "seed-r1-modes"}:
        if args.command not in {"serve", "init-r1", "seed-r1-modes"} or args.state_dir is None:
            parser.error("R1 project commands require an explicit --state-dir")
        if args.r1_project_id is None:
            parser.error("R1 project commands require --r1-project-id")
        if state.resolve() == (args.workspace / "outputs/camera-platform").resolve():
            parser.error("R1 project state must be separate from the primary platform state")
        from .project_mode import configure

        try:
            marker = configure(state, project_id=args.r1_project_id)
        except ValueError as error:
            parser.error(str(error))
        if args.command == "init-r1":
            print(json.dumps({"state_dir": str(state.resolve()), **marker}, ensure_ascii=False))
            return
        if args.command == "seed-r1-modes":
            from .r1_registry_seed import ensure
            from .service import PlatformService

            seeded = ensure(PlatformService(state, args.workspace))
            print(json.dumps(seeded, ensure_ascii=False))
            return
    if args.command == "backup":
        if args.output is None:
            parser.error("backup requires --output for the archive path")
        from .backup import backup_state

        print(json.dumps(
            backup_state(state, args.output, expected_project_id=args.expected_project_id),
            ensure_ascii=False,
        ))
        return
    if args.command == "restore":
        if args.archive is None or args.state_dir is None:
            parser.error("restore requires --archive and a new --state-dir")
        from .backup import restore_state

        print(json.dumps(
            restore_state(
                args.archive, args.state_dir, expected_project_id=args.expected_project_id
            ),
            ensure_ascii=False,
        ))
        return
    if args.command in {"serve", "export-openapi"}:
        from .api import create_app
        from .project_mode import read as read_project_mode

        app = create_app(
            state,
            args.workspace,
            start_worker=not args.external_worker,
            seed_demo=not args.empty and read_project_mode(state) is None,
        )
        if args.command == "export-openapi":
            content = json.dumps(app.openapi(), ensure_ascii=False, indent=2)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(content + "\n")
            else:
                print(content)
        else:
            import uvicorn

            # Local edition intentionally does not expose a public-host option.
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        return
    from .service import PlatformService

    service = PlatformService(state, args.workspace)
    if args.command == "worker":
        from .worker import work

        work(service)
    elif args.command == "demo":
        from .demo import seed

        seed(service)
        data = service.bootstrap()
        runs = [
            service.submit_run({"assembly_id": a, "test_id": data["demo"]["test_id"]})
            for a in data["demo"]["assembly_ids"]
        ]
        print(
            json.dumps(
                {
                    "run_ids": [r["id"] for r in runs],
                    "next": "Start the worker or server to execute queued runs",
                },
                ensure_ascii=False,
            )
        )
    else:
        audit = service.registry.audit()
        for kind in (
            "assets",
            "models",
            "assemblies",
            "tests",
            "measurements",
            "validations",
            "releases",
            "captures",
            "calibrations",
        ):
            service.registry.list(kind)
        runs = service.registry.list_runs()
        artifacts = [a for r in runs for a in r["artifacts"]]
        artifacts += [m["original_artifact"] for m in service.registry.list("measurements")]
        artifacts += [a for c in service.registry.list("captures") for a in c["artifacts"]]
        for artifact in artifacts:
            service.registry.blob_path(artifact["sha256"])
        case_revisions = sum(
            len(service.case_review_history(review["id"]))
            for review in service.list_case_reviews()
        )
        print(
            json.dumps(
                {
                    "audit_events": len(audit["events"]),
                    "artifacts_verified": len(artifacts),
                    "case_review_revisions_verified": case_revisions,
                    "audit_head": audit["head_hash"],
                    "status": "verified",
                }
            )
        )


if __name__ == "__main__":
    main()
