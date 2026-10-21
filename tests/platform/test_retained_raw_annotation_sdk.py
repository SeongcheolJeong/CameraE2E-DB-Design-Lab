"""SDK routes preserve the server's source-bound annotation contract."""

from camera_platform.retained_raw_dataset import RAW_ANNOTATION_SCHEMA
from camera_platform.sdk import PlatformClient


def test_labeled_raw_annotation_template_and_import_sdk_routes():
    client = object.__new__(PlatformClient)
    calls = []
    client.get = lambda path: calls.append(("GET", path)) or path
    client.post = lambda path, payload: calls.append(("POST", path, payload)) or payload

    assert client.retained_raw_dataset_template() == (
        "retained-raw-datasets/template?source_kind=saved_sensor_quality"
    )
    assert client.retained_raw_dataset_template(
        source_kind="lens_sensor_raw_labeled", source_job_id="run-123"
    ) == (
        "retained-raw-datasets/template?"
        "source_kind=lens_sensor_raw_labeled&source_job_id=run-123"
    )
    document = {"schema": RAW_ANNOTATION_SCHEMA}
    assert client.import_retained_raw_annotation(document) == {"document": document}
    assert client.import_retained_raw_annotation(document, expected_sha256="a" * 64) == {
        "document": document,
        "expected_sha256": "a" * 64,
    }
    assert calls[-2:] == [
        ("POST", "retained-raw-datasets/annotations", {"document": document}),
        (
            "POST",
            "retained-raw-datasets/annotations",
            {"document": document, "expected_sha256": "a" * 64},
        ),
    ]
