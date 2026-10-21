#!/usr/bin/env python3
"""Serve saved decision evidence without opening any registry or Worker.

Use --serve for the loopback view. Without it, print the current saved choices.
No acquisition, filter, calibration fit, model publication or write API exists.
"""

from pathlib import Path
import argparse
import json
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from camera_platform.decision_review import DecisionReview, install_routes


def create_review_app(root=ROOT):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse

    app = FastAPI(title="CameraE2E saved decision review", docs_url=None, redoc_url=None,
                  openapi_url=None)
    static = ROOT / "src/camera_platform/static"
    @app.middleware("http")
    async def read_only_boundary(request, call_next):
        try:
            hostname = urlsplit("http://" + request.headers.get("host", "")).hostname
        except ValueError:
            hostname = None
        if hostname not in {"localhost", "127.0.0.1", "testserver"}:
            return JSONResponse({"error": "Loopback host only"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != "http://" + request.headers.get("host", ""):
            return JSONResponse({"error": "Cross-origin requests are not allowed"}, status_code=403)
        if request.method not in {"GET", "HEAD"}:
            return JSONResponse({"error": "Saved decision view is read-only"}, status_code=405)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        })
        return response

    install_routes(app, root, workbench_url="http://127.0.0.1:8770/#experiments")
    @app.get("/")
    def index():
        return FileResponse(static / "decision-review.html")

    @app.get("/{filename}")
    def file(filename: str):
        if filename not in {"decision-review.html", "decision-review.js", "decision-review.css"}:
            raise HTTPException(404)
        return FileResponse(static / filename)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8797)
    args = parser.parse_args()
    if args.port == 8770 or not 1024 <= args.port <= 65535:
        parser.error("Use an unprivileged development port other than primary 8770")
    catalog = DecisionReview(ROOT).catalog()
    print(json.dumps({"read_only": True, "saved_decisions": [
        {"id": r["id"], "choice": r["selected_candidate_id"], "cases": len(r["cases"])}
        for r in catalog["decisions"]], "unavailable": catalog["unavailable"],
        "url": f"http://127.0.0.1:{args.port}/decision-review.html"}, ensure_ascii=False), flush=True)
    if args.serve:
        import uvicorn
        uvicorn.run(create_review_app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
