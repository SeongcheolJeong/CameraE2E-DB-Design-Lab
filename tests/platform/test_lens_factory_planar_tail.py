"""A certified planar tail narrows quadrature without widening physical claims."""

import copy
import math
from pathlib import Path

import pytest

from camera_platform import lens_factory
from camera_platform.lens_db_wavefront_bridge import (
    APERTURE_HYPOTHESIS,
    db_wavefront_preflight,
)

ROOT = Path(__file__).resolve().parents[2] / "camerae2e_db"
LENSES = [
    (
        "6c48e967-74b4-5901-8186-ba6fafe4797b",
        "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd",
    ),
    (
        "fd959d9d-8503-5fe4-a582-4cc9074e6bfd",
        "139808e7a87c11d4e6559862d5231eba156509d2c00b2dd1b69a705aaa5546ef",
    ),
    (
        "444be61b-3bbb-51f9-938a-c4823e702d13",
        "c7df3861b3d9fe6b7fde2af3575322b37306675bbb556b49fafbc498919bbf15",
    ),
]


def _prescription(revision_id, sha256):
    preflight = db_wavefront_preflight(
        revision_id,
        catalog_root=ROOT,
        reviewed_csv_sha256=sha256,
        aperture_hypothesis=APERTURE_HYPOTHESIS,
    )
    assert preflight["status"] == "research_request_ready"
    return preflight["psf_request"]["prescription"]


@pytest.mark.parametrize("revision_id,sha256", LENSES)
def test_reviewed_planar_tail_has_explicit_admitted_ray_support(revision_id, sha256):
    prescription = lens_factory.validate_request(_prescription(revision_id, sha256))
    points, directions, weights, distance = lens_factory._sampling(prescription, 1)
    support = lens_factory._angular_support(prescription, distance)
    certificate = support["certificate"]
    assert certificate["schema"] == "camera_planar_air_tail_support_v3"
    assert certificate["limiting_surface_id"] == "db-surface-24-24"
    assert certificate["minimum_air_gap_mm"] > 0
    assert certificate["maximum_admitted_sensor_angle_deg"] < 80
    assert certificate["sampling_maximum_angle_deg"] < 80
    assert certificate["full_rear_disk_maximum_angle_deg"] > 80
    assert certificate["physical_validation"] == "not_established"
    assert points.shape[:2] == directions.shape[:2] == weights.shape
    for field, height in enumerate(prescription["image_height_mm"]):
        centered = points[field] - [height, 0.0]
        assert (centered**2).sum(axis=-1).max() <= support["disk_radius_mm"] ** 2
        area = (weights[field] * distance**2 / directions[field, :, 2] ** 4).sum()
        assert area == pytest.approx(math.pi * support["disk_radius_mm"] ** 2)


def test_curved_tail_cannot_inherit_planar_air_certificate():
    prescription = lens_factory.validate_request(_prescription(*LENSES[0]))
    _, _, _, distance = lens_factory._sampling(prescription, 1)
    altered = copy.deepcopy(prescription)
    altered["surfaces"][-2]["profile"] = "spherical"
    altered["surfaces"][-2]["radius_mm"] = 100.0
    support = lens_factory._angular_support(altered, distance)
    assert support["certificate"] is None
    assert support["maximum_sample_angle_deg"] > 80
