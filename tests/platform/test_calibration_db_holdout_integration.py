"""Independent-reference eligibility and fresh acquisition boundaries."""

from copy import deepcopy

import pytest

from camera_platform import calibration_db_holdout as holdout
from camera_platform.calibration_color_demo import template


@pytest.fixture(scope="module")
def authored():
    return template(104173)


def test_only_known_authored_input_can_claim_virtual_truth(authored):
    assert holdout.plan(authored, 4917)["status"] == "planned"
    modified = deepcopy(authored)
    modified["ccm_validation"]["target_rgb"][0][0] += 0.01
    assert holdout.plan(modified, 4917)["status"] == "not_evaluated"
    modified = deepcopy(authored)
    modified["qe_scan"]["source_kind"] = "measured"
    assert holdout.plan(modified, 4917)["status"] == "not_evaluated"
    assert holdout.plan({})["status"] == "not_evaluated"


def test_display_metadata_does_not_change_reference(authored):
    renamed = {**authored, "name": "My session", "campaign_id": "my-campaign"}
    assert holdout.plan(renamed, 4917)["status"] == "planned"


@pytest.mark.parametrize("seed", [-1, True, 2**31, 1.5])
def test_seed_domain_is_explicit(authored, seed):
    with pytest.raises(ValueError, match="seed"):
        holdout.plan(authored, seed)


def test_independent_sampler_does_not_call_prediction_capture(monkeypatch):
    import numpy as np

    def forbidden(*_a, **_k):
        raise AssertionError("Prediction sampler must not generate the reference")

    monkeypatch.setattr(holdout.engine.charts, "_capture", forbidden)
    readout = dict(
        dark_current_e_per_s=10,
        full_well_e=10000,
        read_noise_e=10,
        black_level_dn=64,
        dn_per_e=0.4031,
        bit_depth=12,
    )
    rate = np.full((32, 32), 500000.0)
    a = holdout.reference_raw(rate, readout, 0.001, 71)
    assert np.array_equal(a, holdout.reference_raw(rate, readout, 0.001, 71))
    assert not np.array_equal(a, holdout.reference_raw(rate, readout, 0.001, 72))
    assert a.min() >= 0 and a.max() <= 4095
