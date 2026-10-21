"""Explicit Sensor response conversion templates; no invented site anisotropy."""

from copy import deepcopy

import numpy as np

from .contracts import ModelCreate
from .native_model_evidence import verify_models
from .native_pixel_response import validate_response


def native_model_template(service, base_sensor_model_id):
    base = service.registry.get("models", base_sensor_model_id)
    if base["kind"] != "sensor":
        raise ValueError("Native pixel template requires an explicit base Sensor model")
    verify_models(service, [base])
    payload = base["payload"]
    if "native_pixel_response" in payload:
        response = deepcopy(payload["native_pixel_response"])
        response["source"] = {
            "kind": "estimated", "description": "Editable copy of " + base["id"]
            + "; no new acquisition or physical validation",
        }
    else:
        angular = payload["sensor_angular_response"]
        absolute = (np.asarray(payload["qe"])[..., None]
                    * np.asarray(angular["relative_response"]))
        sites = absolute[[0, 1, 1, 2]].transpose(1, 2, 0)
        values = np.repeat(sites[:, :, None, :], 4, axis=2)
        response = {
            "schema": "camera_native_pixel_response_v1",
            "site_names": ["R", "Gr", "Gb", "B"],
            "site_index_tile": [[0, 1], [2, 3]],
            "pixel_pitch_um": payload["pixel_pitch_um"],
            "reference_plane": "sensor_surface_before_pixel_optics",
            "direction_convention": "propagation_into_sensor_x_column_y_row",
            "polarization": "unpolarized",
            "response_basis": "independent_site_collected_electrons_per_incident_pixel_photon",
            "wavelength_nm": payload["wavelength_nm"], "theta_deg": angular["theta_deg"],
            "phi_deg": [0, 90, 180, 270],
            "interpolation": "linear_wavelength_theta_periodic_phi",
            "values_axis_order": ["wavelength", "theta", "phi", "site"],
            "values": values.tolist(),
            "source": {"kind": "estimated", "description": "Derived from " + base["id"]
                       + ": absolute qe * relative angular response; Gr=Gb; phi-invariant. "
                       "No new site/azimuth evidence or physical validation."},
        }
    response = validate_response(response)
    model = {key: deepcopy(value) for key, value in base.items() if key in ModelCreate.model_fields}
    model.update(name=base["name"] + " · Native 4-site 후보", revision="native-1",
                 source_kind="estimated", source_uri="derived-model://" + base["id"],
                 method_and_version="native-independent-site-response-template:1",
                 parents=[base["id"]], physical_instance=None,
                 factory_job_id=None, psf_factory_job_id=None,
                 factory_application_conditions=None,
                 parameter_uncertainty={}, uncertainty={"kind": "unknown"})
    model["payload"] = {"wavelength_nm": response["wavelength_nm"],
                        "pixel_pitch_um": response["pixel_pitch_um"],
                        "readout": deepcopy(payload["readout"]),
                        "native_pixel_response": response}
    if "charge_collection" in payload:
        # Copy an explicitly supplied generation/collection pair. A collected
        # EQE table cannot establish generation yield or collection efficiency.
        model["payload"]["charge_collection"] = deepcopy(payload["charge_collection"])
        model["method_and_version"] = "native-generation-collection-copy-template:1"
    model = ModelCreate.model_validate(model).model_dump()
    # Do not silently widen or repair the base model's physical declarations.
    # The returned draft must satisfy the same payload contract as registration.
    service._validate_model_payload(model)
    return model
