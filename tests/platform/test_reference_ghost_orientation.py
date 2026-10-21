"""Physical receiver orientation through the actual job and CFA selection."""

import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_reference_ghost_job import execute
from test_reference_ghost_job import fixture as fixture

from camera_platform import reference_ghost_engine as engine
from camera_platform import reference_ghost_job as job
from camera_platform.sdk import PlatformClient


@pytest.mark.parametrize("version", [job.LEGACY_SCHEMA, job.SCHEMA])
def test_native_rows_and_cfa_sites_preserve_their_independent_coordinates(
    fixture, monkeypatch, version
):
    app, saved = fixture
    spec = job.template(version)
    spec.update(
        source=saved["source"],
        fields=["center"],
        charts=["uniform"],
        angles_deg=[15.0],
        coatings=["r005"],
        irradiance_w_m2=[1.0],
    )
    spec["isp"]["lsc_enabled"] = False
    original_map = engine.map_angle

    def asymmetric_map(*args):
        directory = original_map(*args)
        rate = np.zeros((2, 256, 256, 3, 4))
        physical_row = np.arange(256)[:, None]
        for site in range(4):
            rate[0, :, :, :, site] = (100 * (site + 1) + physical_row)[..., None]
        np.savez(directory / "rates.npz", center=rate)
        return directory

    monkeypatch.setattr(engine, "map_angle", asymmetric_map)
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        client = object.__new__(PlatformClient)
        client.http = http
        summary = execute(app, client, spec)["result"]["summary"]
        artifact = http.get(summary["files_base"] + "cases/case-000/mean-rates.npz")
        assert artifact.status_code == 200
        with np.load(io.BytesIO(artifact.content)) as saved_rates:
            mosaic = saved_rates["direct"]
    for site, (r, c) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
        rows = np.arange(r, 256, 2)
        physical_rows = 255 - rows if version == job.SCHEMA else rows
        expected = np.broadcast_to((100 * (site + 1) + physical_rows)[:, None], (128, 128))
        np.testing.assert_array_equal(mosaic[r::2, c::2], expected)
    assert summary["new_readout_frames"] == (24 if version == job.SCHEMA else 16)
