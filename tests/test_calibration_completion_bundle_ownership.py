"""Deduplicated ZIP bytes retain and verify every typed measurement owner."""
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from camera_platform import calibration_completion as completion
from camera_platform import calibration_lab as lab
from camera_platform.registry import IntegrityError, digest


def bundle_fixture(tmp_path, monkeypatch, *, corrupt_owner=None, missing_owner=None, changed_owner=None):
    data = b"same exact original RAW bytes"
    checksum = hashlib.sha256(data).hexdigest()
    blob = tmp_path / checksum
    blob.write_bytes(data)
    rows = {
        identity: {"id": identity, "artifacts": [{"sha256": checksum, "bytes": len(data),
                  "name": name, "media_type": "application/octet-stream"}]}
        for identity, name in (("measurement-training", "train.raw"),
                               ("measurement-validation", "validation.raw"))
    }
    calls = []

    class Registry:
        def get(self, kind, identity):
            assert kind == lab.MEASUREMENTS
            if identity == missing_owner:
                raise KeyError(identity)
            row = deepcopy(rows[identity])
            if identity == changed_owner:
                row["changed"] = True
            return row

        def artifact_binding(self, identity, *, owner_kind, owner_id):
            assert identity == checksum and owner_kind == lab.MEASUREMENTS
            calls.append(owner_id)
            if owner_id == corrupt_owner:
                raise IntegrityError("Missing typed artifact owner binding")
            return {"path": blob}

    runs = {
        "fit-run": {"id": "fit-run", "measurement_snapshots": [deepcopy(rows["measurement-training"])],
                    "result": {"summary": {"files": {}}}},
        "validation-run": {"id": "validation-run", "measurement_snapshots": [deepcopy(rows["measurement-validation"])],
                           "result": {"summary": {"files": {}}}},
    }
    record = {"id": "completion-report", "study_id": "fit-run", "validation_id": "validation-run",
              "files": {}, "study_result_sha256": digest(runs["fit-run"]["result"]),
              "validation_result_sha256": digest(runs["validation-run"]["result"])}
    monkeypatch.setattr(completion, "get", lambda service, identity: deepcopy(record))
    monkeypatch.setattr(lab, "get", lambda service, identity: deepcopy(runs[identity]))
    return SimpleNamespace(registry=Registry()), checksum, data, calls


def test_shared_bytes_written_once_but_both_owners_verified(tmp_path, monkeypatch):
    service, checksum, data, calls = bundle_fixture(tmp_path, monkeypatch)
    path = completion.bundle(service, "completion-report")
    try:
        with ZipFile(path) as archive:
            manifest = json.loads(archive.read("originals-manifest.json"))
            assert manifest["unique_blob_count"] == 1
            references = manifest["references"]
            assert len(references) == 2
            assert {r["measurement_id"] for r in references} == {
                "measurement-training", "measurement-validation"}
            assert {r["original_name"] for r in references} == {"train.raw", "validation.raw"}
            assert {r["archive_path"] for r in references} == {"originals/by-sha256/" + checksum}
            assert archive.read(references[0]["archive_path"]) == data
            assert len([name for name in archive.namelist() if name.startswith("originals/by-sha256/")]) == 1
        assert calls == ["measurement-training", "measurement-validation"]
    finally:
        path.unlink()


def test_duplicate_sha_does_not_bypass_second_owner_binding(tmp_path, monkeypatch):
    service, _, _, calls = bundle_fixture(tmp_path, monkeypatch, corrupt_owner="measurement-validation")
    with pytest.raises(IntegrityError, match="owner binding"):
        completion.bundle(service, "completion-report")
    assert calls == ["measurement-training", "measurement-validation"]


@pytest.mark.parametrize("problem", ["missing_owner", "changed_owner"])
def test_missing_or_changed_current_measurement_cannot_export_frozen_reference(tmp_path, monkeypatch, problem):
    service, _, _, _ = bundle_fixture(tmp_path, monkeypatch, **{problem: "measurement-validation"})
    with pytest.raises((KeyError, IntegrityError)):
        completion.bundle(service, "completion-report")
