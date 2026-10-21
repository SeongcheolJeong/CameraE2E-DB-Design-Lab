"""Report-scoped extraction contracts, without regenerating the sensor DB."""

import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_script(name):
    path = Path(__file__).resolve().parents[2] / "simulations" / "fdtd_tcad" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def optical():
    return _load_script("build_optical_qe_db")


@pytest.fixture(scope="module")
def sensor_db():
    return _load_script("build_sensor_db_from_techinsights")


# Page-backed snippets independently checked in the locally licensed reports:
# DEF-2011-804 p9, DEF-1807-801 p7, DEF-2206-805 p10.
_DTI_SNIPPETS = {
    "DEF-2011-804": [
        "Partial depth dielectric filled B-DTI in use in active pixel array",
        "2.0 µm deep B-DTI structures, 2.4 µm maximum thickness at intersections",
        "Full depth B-DTI used to isolate bond pad windows and in the die seal",
    ],
    "DEF-1807-801": [
        "Full front deep trench isolation F-DTI used for bond pad isolation and in the die seal",
    ],
    "DEF-2206-805": [
        "Full depth F-DTI used to isolate pixels, bond pad windows and in the die seal",
    ],
}


@pytest.mark.parametrize(
    "text",
    [
        "CFA measured thicknesses range from 450 nm to 550 nm "
        "(dependent on color and measured location)",
        "CFA measured thicknesses range from 420 nm to 530 nm "
        "(dependent on color and measured location)",
        "Nanopillar diameters range from 120 nm to 180 nm to improve spectral response",
        "The AR layer thickness ranges from 13 nm to 44 nm",
        "Spectral sensitivity is discussed. Nanopillar diameter covers 120 nm to 180 nm",
        "The optical wavelength range is discussed; CFA thickness ranges from 450 nm to 550 nm",
    ],
)
def test_physical_dimensions_never_become_spectral_ranges(optical, text):
    assert optical.extract_spectral_ranges(text, "local_report.pdf") == []


def test_real_spectral_range_is_kept_separate_from_adjacent_geometry(optical):
    text = (
        "CFA thickness ranges from 450 nm to 550 nm. "
        "The spectral response covers 400 nm to 1000 nm; "
        "nanopillar diameters range from 120 nm to 180 nm."
    )
    result = optical.extract_spectral_ranges(text, "local_report.pdf")
    assert [(row["low_nm"], row["high_nm"]) for row in result] == [(400, 1000)]
    assert result[0]["measurement_verified"] is False
    assert "CFA thickness" not in result[0]["context"]


def test_decimal_wavelengths_are_not_split_into_sentences(optical):
    result = optical.extract_spectral_ranges(
        "Wavelengths range from 0.4 µm to 1.0 µm",
        "local_report.pdf",
    )
    assert [(row["low_nm"], row["high_nm"]) for row in result] == [(400, 1000)]


def test_promotional_qe_points_remain_claims_not_measured_spectra(optical, sensor_db):
    record = sensor_db.new_record("SYNTHETIC")
    record["description_excerpt"] = "Manufacturer claims QE reaches 85% at 850 nm"
    qe = optical.extract_qe_and_spectral(record)
    assert qe["measurement_data_status"] == "reported_qe_points"
    assert qe["quantitative_points"][0]["qe_percent"] == 85
    assert qe["quantitative_points"][0]["measurement_verified"] is False
    assert qe["measured_spectrum_available"] is False
    assert qe["measured_accuracy_verified"] is False
    assert qe["calibration_eligible"] is False


@pytest.mark.parametrize(
    "text",
    [
        "QE is discussed elsewhere. CFA transmission is 85% at 550 nm",
        "Quantum efficiency is improved; reflectance is 5% at 550 nm",
        "Spectral sensitivity is 85% at 550 nm relative to the peak",
        "QE is discussed. The 550 nm CFA thickness occupies 85% of the optical stack",
    ],
)
def test_nearby_non_qe_percentages_are_not_promoted(optical, text):
    assert optical.extract_qe_points_from_context(text, "local_report.pdf") == []


@pytest.mark.parametrize(
    "code,expected",
    [
        ("DEF-2011-804", "partial_depth_back_dti"),
        ("DEF-1807-801", ""),
        ("DEF-2206-805", "full_depth_front_dti"),
    ],
)
def test_report_dti_scope_does_not_confuse_active_array_and_die_periphery(
    sensor_db, code, expected
):
    text = "\n".join(_DTI_SNIPPETS[code])
    assert sensor_db.classify_dti(text)["dti_type"] == expected
    record = sensor_db.new_record(code)
    record["metadata"]["device_type"] = "CMOS Image Sensor"
    record["_text_sources"] = [
        {
            "kind": "pdf_design_snippet",
            "source_file": f"{code}.pdf",
            "source_page": 9,
            "source_sha256": "a" * 64,
            "text": text,
        }
        for text in _DTI_SNIPPETS[code]
    ]
    sensor_db.derive_specs(record)
    assert record["derived_specs"].get("dti_type", "") == expected
    if expected:
        assert record["derived_specs"]["has_dti"] is True
    else:
        assert "has_dti" not in record["derived_specs"]
        assert sensor_db.classify_features(text)["has_dti"] is None
    if expected:
        assert record["derived_evidence"]["dti_type"][0]["source_page"] == 9
        assert record["derived_evidence"]["dti_type"][0]["source_sha256"] == "a" * 64


def test_front_dti_acronym_alone_does_not_imply_full_depth(sensor_db):
    assert (
        sensor_db.classify_dti("F-DTI is used for active pixel array isolation")["dti_type"]
        == "front_dti"
    )
    assert (
        sensor_db.classify_dti("Partial depth F-DTI isolates pixels")["dti_type"]
        == "partial_depth_front_dti"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Full depth F-DTI is used in the die seal and bond pad windows",
        "The prior sensor used full depth F-DTI to isolate pixels",
        "No F-DTI is used in the active pixel array",
        "The active pixel array is shown. Full F-DTI is used in the die seal",
        "Bond pad F-DTI structures are adjacent to the active pixel array",
        "Both full and partial depth B-DTI are used in the active pixel array",
    ],
)
def test_unscoped_peripheral_negated_or_comparative_dti_stays_unknown(sensor_db, text):
    assert sensor_db.classify_dti(text)["dti_type"] == ""


def test_conflicting_active_array_dti_is_recorded_without_first_match_winning(sensor_db):
    record = sensor_db.new_record("SYNTHETIC")
    record["_text_sources"] = [
        {
            "kind": "pdf_design_snippet",
            "source_file": "report.pdf",
            "source_page": page,
            "text": text,
        }
        for page, text in (
            (3, "Partial depth B-DTI isolates pixels"),
            (8, "Full depth F-DTI isolates pixels"),
        )
    ]
    sensor_db.derive_specs(record)
    assert "dti_type" not in record["derived_specs"]
    assert {row["value"] for row in record["derived_ambiguities"]["dti_type"]} == {
        "partial_depth_back_dti",
        "full_depth_front_dti",
    }
    redacted = sensor_db.clean_record_for_output(
        record,
        include_source_paths=False,
        include_evidence_text=False,
    )
    for item in redacted["derived_ambiguities"]["dti_type"]:
        assert item["source_file"].startswith("redacted:")
        assert item["text"].startswith("redacted:")
        assert item["source_page"] in (3, 8)


def test_peripheral_dimensions_do_not_leak_across_clause_into_active_dti(sensor_db):
    record = sensor_db.new_record("SYNTHETIC")
    record["_text_sources"] = [
        {
            "kind": "pdf_design_snippet",
            "source_file": "report.pdf",
            "source_page": 10,
            "text": (
                "Partial depth B-DTI is used in the active pixel array. "
                "Full depth F-DTI in the die seal is 6.0 µm deep"
            ),
        }
    ]
    sensor_db.derive_specs(record)
    assert record["derived_specs"]["dti_type"] == "partial_depth_back_dti"
    assert "dti_depth_um" not in record["derived_specs"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Tungsten contacts are used. The optical grid is TiN", "tin"),
        ("Tungsten contacts below the CFA and a TiN optical grid above it", "tin"),
        ("The optical grid is made of tungsten", "w"),
        ("A W light shield surrounds the color filters", "w"),
        ("A tungsten optical grid separates CFA filters", "w"),
        ("Tungsten BEOL contacts and an optical grid are present", ""),
        ("A low refractive index microlens covers the optical grid", ""),
        ("A low refractive index optical grid separates the CFA filters", "lri"),
    ],
)
def test_optical_grid_material_needs_a_direct_same_clause_relationship(sensor_db, text, expected):
    assert sensor_db.classify_optical_features(text)["grid_material"] == expected


def test_page_hash_provenance_survives_pdf_catalog_and_optical_model(
    sensor_db,
    optical,
    monkeypatch,
    tmp_path,
):
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(b"synthetic PDF byte identity; page extraction is mocked")
    texts = [
        "Pixel pitch 2.9 µm in the active image sensor array",
        "CFA measured thicknesses range from 450 nm to 550 nm "
        "(dependent on color and measured location)",
    ]
    fake_reader = SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda t=t: t) for t in texts]
    )
    monkeypatch.setattr(sensor_db, "PdfReader", lambda path: fake_reader)
    snippets, pages = sensor_db.extract_pdf_snippets(pdf)
    assert pages == 2
    assert [item["source_page"] for item in snippets] == [1, 2]
    expected_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert all(item["source_sha256"] == expected_hash for item in snippets)
    record = sensor_db.new_record("SYNTHETIC")
    record["_text_sources"] = [
        {"kind": "pdf_design_snippet", "source_file": str(pdf), **item} for item in snippets
    ]
    sensor_db.derive_specs(record)
    cleaned = sensor_db.clean_record_for_output(
        record, include_source_paths=True, include_evidence_text=True
    )
    field = optical.representative_cfa_thickness(cleaned)
    assert field["value"] == 0.5
    assert field["source_page"] == 2
    assert field["source_sha256"] == expected_hash
    assert field["source_kind"] == "derived_from_extracted_range"
    proxy = optical.proxy_thickness_field({"representative_thickness_um": field}, {})
    assert proxy["source_page"] == 2
    assert proxy["source_sha256"] == expected_hash


def test_dti_depth_and_width_in_same_sentence(sensor_db):
    text = 'The F-DTI is 6.6 μm thick and has an average width of about 0.23 μm, yielding an aspect ratio of 28.7'
    assert sensor_db.dti_depth_value(text) == 6.6
    assert sensor_db.dti_width_nm_value(text) == 230
    assert sensor_db.dti_depth_value('The DTI has a width of 0.23 μm') is None


def test_ignore_appledouble_pdf(sensor_db, tmp_path):
    folder = tmp_path / 'techinsights_html_download/html/pdfs/example'
    folder.mkdir(parents=True)
    (folder / 'source.pdf').touch()
    (folder / '._source.pdf').touch()
    assert [p.name for p in sensor_db.iter_pdf_files(tmp_path)] == ['source.pdf']
