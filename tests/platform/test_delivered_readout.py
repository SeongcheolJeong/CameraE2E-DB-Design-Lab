"""Delivered read-node conversion without population generation or history claims.

Golden fingerprints were taken before extracting the encode-only boundary. They
bind every old result, array ordering, and the exact main RNG tail. The old
nonlinear-pair model expands COMMON_KEYS from a set; only that unspecified
dictionary key group is canonicalized across Python hash seeds.
"""

import hashlib
from copy import deepcopy

import numpy as np
import pytest
from test_nonlinear_dcg import model as nonlinear_pair_model
from test_nonlinear_readout import model as nonlinear_single_model

from camera_platform import dcg_electrical, electrical, nonlinear_dcg, nonlinear_readout
from camera_platform.registry import digest

MODULES = {
    "linear_single": electrical,
    "nonlinear_single": nonlinear_readout,
    "linear_pair": dcg_electrical,
    "nonlinear_pair": nonlinear_dcg,
}


def cases():
    legacy = {
        "full_well_e": 2000.0,
        "read_noise_e": 3.0,
        "dark_current_e_per_s": 2.0,
        "gain": 1.0,
        "bit_depth": 16,
        "black_level_dn": 1000.0,
    }
    models = {
        "linear_single": electrical.default_electrical_model(legacy),
        "linear_pair": dcg_electrical.default_model(legacy),
        "nonlinear_single": nonlinear_single_model(),
        "nonlinear_pair": nonlinear_pair_model(),
    }
    result = []
    for family, base in models.items():
        for noise in (False, True):
            for variant in ("zero", "full", "rank_one", "negative_rank_one", "second_only"):
                model = deepcopy(base)
                model["fixed_pattern"] = {"prnu_sigma": 0.08, "dsnu_e_per_s": 0.7, "seed": 39}
                model.update(dark_current_e_per_s=2.0, dark_current_doubling_c=5.0)
                if family == "linear_single":
                    model.update(
                        read_noise_e=0.0 if variant in ("zero", "second_only") else 3.0,
                        post_gain_noise_v=0.001
                        if variant in ("full", "rank_one", "second_only")
                        else 0.0,
                        digital_gain=0.7 if variant == "rank_one" else 1.5,
                    )
                elif family == "nonlinear_single":
                    model["read_noise"]["variance_v2"] = 0.0 if variant == "zero" else 4e-8
                    model["digital_gain"] = 0.7 if variant == "rank_one" else 1.5
                else:
                    covariance = {
                        "zero": [[0.0, 0.0], [0.0, 0.0]],
                        "full": [[4.0, 1.0], [1.0, 9.0]],
                        "rank_one": [[4.0, 6.0], [6.0, 9.0]],
                        "negative_rank_one": [[4.0, -6.0], [-6.0, 9.0]],
                        "second_only": [[0.0, 0.0], [0.0, 9.0]],
                    }[variant]
                    if family == "nonlinear_pair":
                        # Exact binary-scaled PSD preserves the rank-one cases.
                        covariance = [[v * 2**-28 for v in row] for row in covariance]
                    key = "covariance_v2" if family == "nonlinear_pair" else "covariance_e2"
                    model["read_noise"][key] = covariance
                    for i, path in enumerate(model["paths"]):
                        path["digital_gain"] = (1.5, 0.7)[i]
                if variant == "negative_rank_one":
                    for path in model["paths"] if "pair" in family else [model]:
                        path["adc"].update(black_level_dn=0.0, full_scale_v=0.12)
                result.append((f"{family}:{noise}:{variant}", family, model, noise))
    return result


def fingerprint(value):
    if isinstance(value, np.ndarray):
        return {
            "shape": list(value.shape),
            "dtype": value.dtype.str,
            "sha256": hashlib.sha256(value.tobytes(order="C")).hexdigest(),
        }
    if isinstance(value, dict):
        ordered = list(value)
        common = nonlinear_dcg.COMMON_KEYS
        if value.get("schema") == nonlinear_dcg.SCHEMA and common <= value.keys():
            start = min(ordered.index(key) for key in common)
            ordered = [key for key in ordered if key not in common]
            ordered[start:start] = sorted(common)
        return {"ordered_keys": ordered, "values": {k: fingerprint(v) for k, v in value.items()}}
    if isinstance(value, (tuple, list)):
        return [fingerprint(v) for v in value]
    return value


def old_capture(family, model, noise, monkeypatch):
    module = MODULES[family]
    original = np.random.default_rng
    generators = []

    def tracked(seed):
        rng = original(seed)
        if isinstance(seed, int):
            generators.append(rng)
        return rng

    with monkeypatch.context() as patch:
        patch.setattr(np.random, "default_rng", tracked)
        fn = module.capture_shared_electrons if "pair" in family else module.capture_electrons
        output = fn(
            np.array([[0.0, 0.49, 1.5, 199.9], [999.5, 1199.9, 2000.0, 4000.0]]),
            0.03125,
            model,
            **({"analog_gain": 1.25} if "single" in family else {}),
            temperature_c=25.0,
            geometry={"full_sensor_shape": [8, 10], "crop_origin": [2, 3]},
            noise_enabled=noise,
            seed=7391,
        )
    assert len(generators) == 1
    tail = {
        "normal": generators[0].standard_normal(9),
        "integer": generators[0].integers(0, 2**63, 7, dtype=np.uint64),
    }
    return digest(fingerprint({"output": output, "rng_tail": tail}))


# Bound to the four pre-extraction source SHAs below. The nonlinear-pair
# unordered COMMON_KEYS group was canonicalized using the same-SHA immutable
# reports/camera_platform/aspheric-optics-v016/source-snapshot.zip implementation.
# Never regenerate these values from the refactored capture implementations.
GOLDEN = {
    "linear_single:False:zero": "08a764a0b93d1f97e3ac165d14bfe36c3e225c446fb969d65d92787701ed7566",
    "linear_single:False:full": "80a1fe0c19c2559c5f733af3b80c6a625843c4ed0ef862db654365dfa701bedc",
    "linear_single:False:rank_one": (
        "c6169f9c9592b1f67c97b78e752b76333bf9851ada28d0c535220585938aebe0"
    ),
    "linear_single:False:negative_rank_one": (
        "c233fe156f6f41352dc12a6064db48cb6f3bd6401466b7706cdce26b3868c01c"
    ),
    "linear_single:False:second_only": (
        "bd2e4e2d0a76febf9c31a38e0a6a0eecae6a8114a85675ad32a7a0ef2dd729a4"
    ),
    "linear_single:True:zero": "11dfa1c0756770a29e15ebd3b983ee082c8c625d575fed899f896b1eb0b58208",
    "linear_single:True:full": "48a1e53232af5cbb44a99ee066065c8e55a0c74e50ce687f6a173f7e87ae7013",
    "linear_single:True:rank_one": (
        "63a45bc193cd3e4d89bcb4142e692d767adaa6804f4ba4062ffec84d83ae93a7"
    ),
    "linear_single:True:negative_rank_one": (
        "0c0c6c57fcaecd60aaadb9960abae38967ff0e0895204d2b50cfa7cbd574d387"
    ),
    "linear_single:True:second_only": (
        "6fd728975766b8a11849b0c307850ee8285726013218530002c7c7bc8f664bab"
    ),
    "linear_pair:False:zero": "5f67b0259ccde0aa3654d153583ceaa8c32c38e87cc2c0f031db8a14c6c6c358",
    "linear_pair:False:full": "b8512052ce6b66a53dd82cdb3763dc81dc107b01f9b2d37833f23ae782bb5626",
    "linear_pair:False:rank_one": (
        "8b3df8aa719910586ac63813331d82e3c06d8105e3f25325cefaa212484ec8e9"
    ),
    "linear_pair:False:negative_rank_one": (
        "048d0444979417f5339cda57bea3af116ad7f693f4f79b273f72fe03624ef9d7"
    ),
    "linear_pair:False:second_only": (
        "29b3908db51acce7911c5b17397907de5144edc6711ceafba0776a928ea89268"
    ),
    "linear_pair:True:zero": "e1b073e70ad42345805b31fba9acf0c5b4e1d2724e2239f0d646d9decbeae73d",
    "linear_pair:True:full": "d78f085539c6c92d1c899c7677bc5256e46f3c6d0f5c1f42b4f5f91285810e75",
    "linear_pair:True:rank_one": "6bb7e4ab33d2f9704d731e4f6087746cbe11eaa3abc1864ead400e984e1a8adc",
    "linear_pair:True:negative_rank_one": (
        "e5b493d13a8431e91214dab999305774379cfa5e1459708d5b886f2b663cda28"
    ),
    "linear_pair:True:second_only": (
        "ff38e1ceba1151472500da4cec0a674daaa1ab31466d0d34d019dc4757e2a355"
    ),
    "nonlinear_single:False:zero": (
        "d2cdba2454936cf58f2418f32d46d04d84870f812c5ae403c9ac353476d74bee"
    ),
    "nonlinear_single:False:full": (
        "8c3df9cefcb377f666681d0ee84d55ba4ba3e0a3ee7990dfabaa5af692eee056"
    ),
    "nonlinear_single:False:rank_one": (
        "8d766e236e5d1a5553a99dc97d59c7b4732c57e2e8c2c9f890d9e270eb7bb417"
    ),
    "nonlinear_single:False:negative_rank_one": (
        "1dc1abd95e07a174d646c3f09b7cd5ea37d47c6182dee6409d9be255b2078e97"
    ),
    "nonlinear_single:False:second_only": (
        "8c3df9cefcb377f666681d0ee84d55ba4ba3e0a3ee7990dfabaa5af692eee056"
    ),
    "nonlinear_single:True:zero": (
        "600792b19968b7dcda713fbe0a91e7979206259d337235aa88d6d87eff56b908"
    ),
    "nonlinear_single:True:full": (
        "6a5a90101863c19b78cf1b9f1fc7aa2989490dc8e4a1f74224679a0fd5d2a0b2"
    ),
    "nonlinear_single:True:rank_one": (
        "1c8150b1660e2d7b686f5a2e3b67146c62ddb319c6d426a8eac8a9584e93c629"
    ),
    "nonlinear_single:True:negative_rank_one": (
        "d511065037ee1f6b7cbd98ecf752441a4fe94b00ceb90a8ac819357280157ddf"
    ),
    "nonlinear_single:True:second_only": (
        "6a5a90101863c19b78cf1b9f1fc7aa2989490dc8e4a1f74224679a0fd5d2a0b2"
    ),
    "nonlinear_pair:False:zero": "154c052bdf6e40a7a15c07b047fee2747745b33a176887952002408bfc7a6b92",
    "nonlinear_pair:False:full": "67fd897d38a553ff8ceaaaa61ffa2df5c2c387207c26da6314d015d54b39603d",
    "nonlinear_pair:False:rank_one": (
        "004cc2be8ef97c6f121ecd6f038d36e9be0f034ca8ab1581d381acfcccca81e3"
    ),
    "nonlinear_pair:False:negative_rank_one": (
        "8bf09c2d019859889dd99c98afeae231947e53b84c69bec5d55a92e85c48c4b2"
    ),
    "nonlinear_pair:False:second_only": (
        "25bcfd4194c7a15d0777aed75e63933de24f233de768f906e4c55eb9a65cd86a"
    ),
    "nonlinear_pair:True:zero": "f2b2b478ff2d9ea8af2ffafb822bf73624c4ddff0496a7d448f912dc9d8e3d3b",
    "nonlinear_pair:True:full": "6dfd17c46827ff0844dbe3c38b94f6d17dc3d0bdc4a8633d6e30bbd98df86aa6",
    "nonlinear_pair:True:rank_one": (
        "77c21cb8d9d80aaa8f25e6d94b3aea66893c8a94f54eb7cc6a0aab0049d0667b"
    ),
    "nonlinear_pair:True:negative_rank_one": (
        "13eaf470f091cc29f13af7f1df6f33bde9fe1b263e23ecd4366d2a1be8ea24ee"
    ),
    "nonlinear_pair:True:second_only": (
        "65f8ae9b8259744cfab22b5869cc4fb86373075d50907e5df4491d123894ab33"
    ),
}
GOLDEN_SOURCE_SHA256 = {
    "linear_single": "ec355cda14443dc5108bb4b46556d49d2ab660e1a881600bed34fa73aec52bdb",
    "nonlinear_single": "8c2c9b03a0f964e32eb277b6148aa3a1ff4683bd7fec4fd1e168fe81a4591386",
    "linear_pair": "ae151403a252106a4657bbff1ec8ac110763df6130e8417b53715601ab3aeae5",
    "nonlinear_pair": "18d26cc64ed2694d998583a7bbecb75d72c77893571f5411f8dd79f030b1688b",
}


@pytest.mark.parametrize(
    "identity,family,model,noise", cases(), ids=lambda x: x if isinstance(x, str) else None
)
def test_legacy_capture_results_and_rng_tail_are_bit_exact(
    identity, family, model, noise, monkeypatch
):
    assert old_capture(family, model, noise, monkeypatch) == GOLDEN[identity]


def delivered(family, model, charge, *, noise=False, rng=None, temperature=25.0, analog=1.25):
    return MODULES[family].read_delivered_charge(
        charge,
        model,
        **({"analog_gain": analog} if "single" in family else {}),
        temperature_c=temperature,
        noise_enabled=noise,
        rng=np.random.default_rng(7391) if rng is None else rng,
    )


@pytest.mark.parametrize("identity,family,model,noise", cases())
def test_extracted_boundary_matches_legacy_sampled_population_and_rng_tail(
    identity, family, model, noise, monkeypatch
):
    # Legacy output is separately anchored to the pre-extraction immutable source
    # by GOLDEN. Replay just its delivered population and advanced RNG here.
    module = MODULES[family]
    photo = np.array([[0.0, 0.49, 1.5, 199.9], [999.5, 1199.9, 2000.0, 4000.0]])
    original_rng = np.random.default_rng
    main = []

    def tracked(seed):
        generator = original_rng(seed)
        if isinstance(seed, int):
            main.append(generator)
        return generator

    with monkeypatch.context() as patch:
        patch.setattr(np.random, "default_rng", tracked)
        capture = module.capture_shared_electrons if "pair" in family else module.capture_electrons
        old = capture(
            photo,
            0.03125,
            model,
            **({"analog_gain": 1.25} if "single" in family else {}),
            temperature_c=25.0,
            geometry={"full_sensor_shape": [8, 10], "crop_origin": [2, 3]},
            noise_enabled=noise,
            seed=7391,
        )
    assert len(main) == 1
    rng = original_rng(7391)
    mean = old["arrays"]["mean_total_charge_e"]
    sampled = rng.poisson(mean).astype(float) if noise else mean.copy()
    capacity = model["shared_charge_capacity_e"] if "pair" in family else model["full_well_e"]
    charge = np.clip(sampled, 0, capacity)
    before = charge.copy()
    new = delivered(family, model, charge, noise=noise, rng=rng)
    for key, value in new["arrays"].items():
        np.testing.assert_array_equal(value, old["arrays"][key], err_msg=f"{identity}: {key}")
    np.testing.assert_array_equal(charge, before)
    np.testing.assert_array_equal(rng.standard_normal(31), main[0].standard_normal(31))
    np.testing.assert_array_equal(rng.integers(0, 2**63, 29), main[0].integers(0, 2**63, 29))


class ReadNoiseOnly:
    def __init__(self):
        self.calls = []

    def normal(self, mean, sigma, size):
        self.calls.append(("normal", mean, sigma, size))
        return np.full(size, sigma * 0.25)

    def standard_normal(self, size):
        self.calls.append(("standard_normal", size))
        return np.full(size, 0.25)

    def __getattr__(self, name):
        raise AssertionError(f"Population generation/random API {name} must not be used")


def full_cases():
    return [case for case in cases() if case[0].endswith(":full")]


@pytest.mark.parametrize("identity,family,model,noise", full_cases())
def test_readout_uses_only_injected_read_noise_no_generation_and_preserves_inputs(
    identity, family, model, noise, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Delivered readout must not generate charge, patterns or an RNG")

    monkeypatch.setattr(np.random, "default_rng", forbidden)
    monkeypatch.setattr(electrical, "_patterns", forbidden)
    monkeypatch.setattr(dcg_electrical, "_patterns", forbidden)
    # A non-contiguous readonly caller view must also work without hidden mutation.
    charge = np.arange(24, dtype=float).reshape(4, 6)[:, ::2]
    charge.flags.writeable = False
    before, original_model = charge.copy(), deepcopy(model)
    rng = ReadNoiseOnly()
    out = delivered(family, model, charge, noise=noise, rng=rng)
    np.testing.assert_array_equal(charge, before)
    assert model == original_model
    assert all(not np.shares_memory(charge, value) for value in out["arrays"].values())
    methods = [call[0] for call in rng.calls]
    assert methods == (
        []
        if not noise
        else (
            ["standard_normal"]
            if "pair" in family
            else ["normal", "normal"]
            if family == "linear_single"
            else ["normal"]
        )
    )
    assert set(out) == {"arrays", "readout_descriptor"}
    descriptor = out["readout_descriptor"]
    assert descriptor["schema"] == electrical.DELIVERED_READOUT_SCHEMA
    assert descriptor["source_model_schema"] == model["schema"]
    assert descriptor["input_quantity"] == "delivered_total_read_node_charge_e"
    assert (
        descriptor["inverse_calibration"]
        == "not_generated_no_current_exposure_or_photocharge_claim"
    )
    assert descriptor["population_processing"] == "none_already_sampled_and_capacity_bounded"
    assert "exposure_s" not in descriptor and "dark_e" not in descriptor
    assert descriptor["input_shape"] == list(charge.shape)
    assert out["arrays"]["raw_dn"].shape == (
        (2, *charge.shape) if "pair" in family else charge.shape
    )
    assert out["arrays"]["raw_dn"].dtype == np.int32


@pytest.mark.parametrize("family", MODULES)
@pytest.mark.parametrize(
    "bad",
    [
        [[1.0]],
        np.array([1.0]),
        np.zeros((0, 2)),
        np.array([[True]]),
        np.array([[1 + 2j]]),
        np.array([[np.nan]]),
        np.array([[np.inf]]),
        np.array([[-1.0]]),
        np.full((1001, 1000), 0.0),
    ],
)
def test_bad_delivered_population_is_refused_before_read_noise(family, bad):
    model = next(case[2] for case in full_cases() if case[1] == family)
    rng = ReadNoiseOnly()
    with pytest.raises(ValueError, match="delivered_charge_e|Delivered charge"):
        delivered(family, model, bad, noise=True, rng=rng)
    assert rng.calls == []


@pytest.mark.parametrize("family", MODULES)
def test_shared_well_overflow_refused_not_silently_clipped(family):
    model = next(case[2] for case in full_cases() if case[1] == family)
    cap = model["shared_charge_capacity_e"] if "pair" in family else model["full_well_e"]
    charge = np.array([[np.nextafter(cap, np.inf)]])
    rng = ReadNoiseOnly()
    with pytest.raises(ValueError, match="already lie within"):
        delivered(family, model, charge, noise=True, rng=rng)
    assert rng.calls == [] and charge[0, 0] > cap
    # Exact capacity is admitted, with no readout-side common well clipping.
    result = delivered(family, model, np.array([[0.0, cap]]), rng=rng)
    assert result["arrays"]["raw_dn"].dtype == np.int32


@pytest.mark.parametrize("family", MODULES)
@pytest.mark.parametrize("noise", [False, True])
def test_delivered_stage_ignores_unrelated_dark_temperature_generation_law(family, noise):
    model = next(case[2] for case in full_cases() if case[1] == family)
    # This would overflow dark generation at 25 C but cannot affect already
    # delivered population. Readout curve/covariance temperature domains still apply.
    model.update(reference_temperature_c=-273.0, dark_current_doubling_c=1e-6)
    before = np.array([[0.0, 123.0, 999.0]])
    out = delivered(family, model, before, noise=noise)
    assert np.isfinite(out["arrays"]["raw_dn"]).all()
    np.testing.assert_array_equal(before, [[0.0, 123.0, 999.0]])


@pytest.mark.parametrize("family", ["nonlinear_single", "linear_pair", "nonlinear_pair"])
def test_readout_temperature_domain_remains_enforced_with_noise_disabled(family):
    model = next(case[2] for case in full_cases() if case[1] == family)
    model["read_noise"]["temperature_range_c"] = [24.0, 26.0]
    rng = ReadNoiseOnly()
    with pytest.raises(ValueError, match="temperature|Temperature|domain"):
        delivered(family, model, np.array([[10.0]]), rng=rng, temperature=27.0)
    assert rng.calls == []


@pytest.mark.parametrize("family", MODULES)
@pytest.mark.parametrize("bad_noise", [0, 1, None, np.bool_(True)])
def test_noise_flag_requires_python_boolean(family, bad_noise):
    model = next(case[2] for case in full_cases() if case[1] == family)
    with pytest.raises(ValueError, match="noise_enabled"):
        delivered(family, model, np.array([[10.0]]), noise=bad_noise)


@pytest.mark.parametrize("family", MODULES)
def test_missing_rng_interface_is_refused_without_creating_one(family):
    model = next(case[2] for case in full_cases() if case[1] == family)
    with pytest.raises(ValueError, match="rng must provide"):
        delivered(family, model, np.array([[10.0]]), rng=object())


@pytest.mark.parametrize("family", ["linear_single", "nonlinear_single"])
@pytest.mark.parametrize("digital", [0.5, 1.5])
def test_independent_nearest_even_adc_and_digital_boundaries(family, digital):
    model = next(case[2] for case in full_cases() if case[1] == family)
    model["adc"] = {"bit_depth": 3, "full_scale_v": 7.0, "black_level_dn": 0.5}
    model["digital_gain"] = digital
    if family == "linear_single":
        model["conversion_gain_v_per_e"] = 1.0
    else:
        model["charge_voltage_response"]["signal_voltage_v"] = [0.0, 1000.0, 2000.0]
    charge = np.array([[0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 5.5, 6.0, 6.5, 7.0, 20.0]])
    result = delivered(family, model, charge, analog=1.0)["arrays"]
    adc = [min(7, max(0, round(0.5 + q))) for q in charge.flat]
    raw = [min(7, max(0, round(0.5 + digital * (code - 0.5)))) for code in adc]
    np.testing.assert_array_equal(result["adc_dn"], [adc])
    np.testing.assert_array_equal(result["raw_dn"], [raw])
    np.testing.assert_array_equal(result["adc_high_mask"], charge + 0.5 >= 7)


@pytest.mark.parametrize("family", ["linear_pair", "nonlinear_pair"])
def test_path_ceiling_clips_both_readouts_of_same_delivered_population(family):
    model = next(case[2] for case in full_cases() if case[1] == family)
    cap = model["shared_charge_capacity_e"]
    ceiling = model["paths"][0]["input_linear_ceiling_e"]
    charge = np.array([[0.0, ceiling - 1.0, ceiling, ceiling + 1.0, cap]])
    result = delivered(family, model, charge, analog=1.0)["arrays"]
    np.testing.assert_array_equal(result["path_saturated_mask"][0], charge >= ceiling)
    np.testing.assert_array_equal(result["path_saturated_mask"][1], charge >= cap)
    assert result["adc_dn"][0, 0, 2] == result["adc_dn"][0, 0, 3] == result["adc_dn"][0, 0, 4]
    assert result["adc_dn"][1, 0, 2] < result["adc_dn"][1, 0, 4]
    np.testing.assert_array_equal(charge, [[0.0, ceiling - 1.0, ceiling, ceiling + 1.0, cap]])


@pytest.mark.parametrize("family", ["linear_pair", "nonlinear_pair"])
@pytest.mark.parametrize(
    "variant", ["zero", "full", "rank_one", "negative_rank_one", "second_only"]
)
def test_paired_noise_matches_declared_covariance_without_extra_shot_or_read_draws(family, variant):
    model = next(case[2] for case in cases() if case[0] == f"{family}:True:{variant}")
    unit = "v" if family == "nonlinear_pair" else "e"
    expected = np.array(model["read_noise"][f"covariance_{unit}2"])
    # 200k identical delivered populations isolate the read-noise law: no photon
    # statistics, no shared-mean fluctuation and no fitted camera accuracy claim.
    rng = np.random.default_rng(2834)
    untouched = np.random.default_rng(2834)
    out = delivered(family, model, np.full((400, 500), 100.0), noise=True, rng=rng)
    actual = out["arrays"][f"read_noise_{unit}"]
    if variant == "zero":
        assert not np.any(actual)
    else:
        untouched.standard_normal((2, 400, 500))
        measured = np.cov(actual.reshape(2, -1), bias=True)
        scale = np.sqrt(np.diag(expected)[:, None] * np.diag(expected)[None, :])
        assert np.all(np.abs(measured - expected) <= 0.012 * scale)
        if variant in ("rank_one", "negative_rank_one"):
            np.testing.assert_allclose(
                actual[1], (1.5 if variant == "rank_one" else -1.5) * actual[0], atol=1e-14
            )
        if variant == "second_only":
            assert not actual[0].any()
    np.testing.assert_array_equal(rng.standard_normal(13), untouched.standard_normal(13))
