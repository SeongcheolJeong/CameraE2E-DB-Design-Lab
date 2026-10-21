"""Self-consistent file hashes do not permit mismatched camera/solver identities."""
import copy
import json

import pytest

from camera_platform.design_workshop_sensor import _digest as cell_digest
from camera_platform.registry import digest
from camera_platform.sensor_structure_job import sha
from camera_platform.sensor_structure_spectral import task_list
from camera_platform.sensor_structure_spectral_optics import geometry_profile
from camera_platform.sensor_structure_spectral_provenance import validate_prepared


@pytest.fixture
def prepared(tmp_path):
    cell = dict(source_report="DEF-2206-805", pixel_pitch_um=2.9, geometry_um={
        "pitch": 2.9, "si_thickness": 6.6, "lens_height": 1.3, "lens_vertex_radius_um": 1.6,
        "cfa_thickness_by_channel": {"red": .475, "green": .5, "blue": .475},
        "passivation_thickness_by_channel": {"red": .825, "green": .8, "blue": .825}})
    cell["cell_sha256"] = cell_digest(cell)
    profile = geometry_profile()
    chief = {"center": {450.: 0., 550.: 0., 650.: 0.},
             "right-edge": {450.: 4.519230321611206, 550.: 4.524756324933152, 650.: 4.525141720848977}}
    structure = dict(report_code="DEF-2206-805", revision="frozen-structure", geometry={
        "pitch_um": 2.9, "si_um": 6.6, "ocl_height_um": 1.3, "ocl_radius_um": 1.6,
        "cfa_um": .5, "passivation_um": .8})
    tasks = task_list(structure, chief)
    located = dict(geometry=profile, lens_source={"revision_id": "pinned-patent"},
                   chief_rays={key: [dict(wavelength_nm=wave, signed_x_cra_deg=angle) for wave, angle in angles.items()]
                               for key, angles in [("center", chief["center"]), ("outer-ne", chief["right-edge"]) ]})
    response = dict(cell_sha256=cell["cell_sha256"], wavelength_nm=list(range(450, 651, 10)))
    spectral = dict(cell_sha256=cell["cell_sha256"], pixel_response_sha256=digest(response),
                    wavelength_nm=response["wavelength_nm"], optical_revision="optical-revision",
                    pixel_aperture_applications=1,
                    files=[{"path": "center/uniform/bands.npy", "sha256": "a"*64}])
    optical = dict(revision="optical-revision", status="PROPOSED", fields=[dict(
        id=roi["id"], status="succeeded", image_xy_mm=roi["optical_anchor_image_xy_mm"],
        source_revision_id="pinned-patent", geometry={
            "sensor_timing_profile_sha256": profile["profile_sha256"],
            "roi_origin_active_row_col": roi["origin_row_col"],
            "optical_anchor_active_row_col": roi["optical_anchor_active_row_col"]}) for roi in profile["roi_layout"]])
    contents = {"prepared/located-geometry.json": located, "prepared/optics/result.json": optical,
                "prepared/spectral/spectral-inputs.json": spectral,
                "prepared/spectral/pixel-response.json": response}
    manifest = dict(schema="camera_located_optical_preparation_v1", optical_revision="optical-revision", files={})
    def save():
        for relative, content in contents.items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(content))
            manifest["files"][relative] = sha(path)
        spectral["pixel_response_file_sha256"] = manifest["files"]["prepared/spectral/pixel-response.json"]
        p = tmp_path / "prepared/spectral/spectral-inputs.json"
        p.write_text(json.dumps(spectral))
        manifest["files"]["prepared/spectral/spectral-inputs.json"] = sha(p)
        manifest["files"]["prepared/spectral/center/uniform/bands.npy"] = "a"*64
        (tmp_path / "prepared-manifest.json").write_text(json.dumps(manifest))
    save()
    return tmp_path, cell, tasks, contents, manifest, save


def test_exact_preparation_crosslinks_pass(prepared):
    root, cell, tasks, *_ = prepared
    result = validate_prepared(root, tasks, cell)
    assert result["passed"] and result["task_count"] == 144
    assert result["cell_sha256"] == cell["cell_sha256"]
    assert "not physical or 3D validation" in result["qualification"]


def test_loaded_cell_cannot_keep_stale_digest(prepared):
    root, cell, tasks, *_ = prepared
    cell["geometry_um"]["lens_height"] = 1.31
    with pytest.raises(ValueError, match="loaded host cell content"):
        validate_prepared(root, tasks, cell)


def test_other_valid_cell_cannot_consume_saved_rates(prepared):
    root, cell, tasks, *_ = prepared
    cell["geometry_um"]["lens_height"] = 1.31
    cell["cell_sha256"] = cell_digest({k:v for k,v in cell.items() if k != "cell_sha256"})
    with pytest.raises(ValueError, match="rates and host cell differ"):
        validate_prepared(root, tasks, cell)


def test_individually_hashed_optical_revision_mismatch_rejected(prepared):
    root, cell, tasks, content, _, save = prepared
    content["prepared/optics/result.json"]["revision"] = "other-optical-revision"
    save()
    with pytest.raises(ValueError, match="optical revision differs"):
        validate_prepared(root, tasks, cell)


def test_validly_hashed_other_pixel_response_rejected(prepared):
    root, cell, tasks, content, _, save = prepared
    content["prepared/spectral/pixel-response.json"]["unexpected"] = "another response"
    save()
    with pytest.raises(ValueError, match="pixel response differs"):
        validate_prepared(root, tasks, cell)


def test_virtual_anchor_cannot_be_relabelled_inside_sensor(prepared):
    root, cell, tasks, content, _, save = prepared
    content["prepared/optics/result.json"]["fields"][1]["image_xy_mm"] = [8.01, 8.01]
    save()
    with pytest.raises(ValueError, match="chief misses declared"):
        validate_prepared(root, tasks, cell)


def test_signed_cra_must_match_even_with_new_valid_task_identity(prepared):
    root, cell, tasks, *_ = prepared
    task = next(t for t in tasks if t["field"] == "right-edge")
    task["angle_deg"] *= -1
    task["id"] = digest({k:v for k,v in task.items() if k != "id"})
    with pytest.raises(ValueError, match="signed chief CRA"):
        validate_prepared(root, tasks, cell)


def test_no_return_to_20_degree_virtual_assignment(prepared):
    root, cell, tasks, *_ = prepared
    task = next(t for t in tasks if t["field"] == "right-edge")
    task["angle_deg"] = 20.
    task["id"] = digest({k:v for k,v in task.items() if k != "id"})
    with pytest.raises(ValueError, match="signed chief CRA"):
        validate_prepared(root, tasks, cell)


@pytest.mark.parametrize("name", ["pitch_um", "ocl_height_um", "ocl_radius_um", "si_um"])
def test_meep_geometry_cannot_silently_change(prepared, name):
    root, cell, tasks, *_ = prepared
    task = tasks[0]
    task["structure"]["geometry"][name] *= 1.01
    task["id"] = digest({k:v for k,v in task.items() if k != "id"})
    with pytest.raises(ValueError, match="geometry differs at"):
        validate_prepared(root, tasks, cell)


def test_missing_solver_node_rejected(prepared):
    root, cell, tasks, *_ = prepared
    with pytest.raises(ValueError, match="144-sample"):
        validate_prepared(root, tasks[:-1], cell)


def test_duplicate_solver_coordinate_does_not_count_as_completion(prepared):
    root, cell, tasks, *_ = prepared
    tasks[-1] = copy.deepcopy(tasks[0])
    with pytest.raises(ValueError, match="repeated"):
        validate_prepared(root, tasks, cell)


def test_nested_spectral_array_manifest_cannot_reference_other_bytes(prepared):
    root, cell, tasks, content, _, save = prepared
    content["prepared/spectral/spectral-inputs.json"]["files"][0]["sha256"] = "b"*64
    save()
    with pytest.raises(ValueError, match="array manifest link differs"):
        validate_prepared(root, tasks, cell)
