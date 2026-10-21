# Internal implementation contract

The additive catalog uses `camerae2e_db/catalog.sqlite`. Existing input databases
are read-only. Canonical IDs are UUID5 values derived from namespaced source
identities, never filenames. Do not merge distinct sensor reports by model name.

Importer modules expose `import_lenses(writer, path)` and
`import_sensors(writer, catalog_path, simulation_path=None)`, returning counts and
warnings. They import `CatalogWriter`, `digest`, `stable_id`, `json_text`, and
`file_hash` from `._catalog_store`. Writer owns transaction and output root.

Writer methods (all keyword arguments except those shown positionally):

* `component(family, identity, manufacturer, model, entity_kind)` -> component ID
* `alias(component_id, namespace, alias)`
* `source(kind, identity, *, uri=None, sha256=None, metadata=None)` -> source ID
* `revision(component_id, model_kind, snapshot, *, source_id=None)` -> revision ID
* `operating_point(revision_id, label, conditions)` -> operating point ID
* `observation(component_id, property_name, value, *, unit=None,
  origin_kind='reported', source_id=None, revision_id=None,
  operating_point_id=None, conditions=None, source_locator=None,
  missing_reason=None, extraction_method=None)` -> observation ID
* `run(revision_id, operating_point_id, identity, status, *, solver=None,
  settings=None, source_id=None, input_sha256=None, metadata=None,
  created_at=None)` -> run ID
* `artifact(path, kind, *, run_id=None, revision_id=None,
  expected_sha256=None, metadata=None)` -> artifact ID. Existing files inside the
  output root keep relative references; outside files are copied into
  content-addressed artifacts. Missing paths remain registered as unavailable.
* `validation(target_type, target_id, gate, status, details=None)` -> check ID.
  Status is passed/failed/unknown; historical validation stays in details and
  must not be promoted into freshly verified scientific accuracy.
* `put(table, **values)` -> insert row using supplied column names (idempotent).
* `writer.conn`: SQLite connection. `writer.root`: resolved output root Path.

Typed tables available to importers:

* `lens_configurations(operating_point_id PK, focal_length_mm REAL, f_number REAL,
  image_height_mm REAL, image_circle_mm REAL, metadata_json TEXT)`
* `lens_surfaces(revision_id, surface_order, radius_mm, thickness_mm,
  aperture_mm, material, nd, vd, conic, coefficients_json, metadata_json)`;
  primary key `(revision_id, surface_order)`.
* `sensor_geometry(revision_id PK, pixel_pitch_um REAL, width_px INTEGER,
  height_px INTEGER, active_width_mm REAL, active_height_mm REAL,
  cfa_pattern TEXT, metadata_json TEXT)`
* `sensor_layers(revision_id, layer_order INTEGER, name TEXT, material TEXT,
  thickness_um REAL, metadata_json TEXT)`; PK `(revision_id, layer_order)`.
* `sensor_readout_modes(operating_point_id PK, mode_name TEXT, bit_depth INTEGER,
  metadata_json TEXT)`.

Do not invent unknown dimensions, mode conditions, material dispersions or
coefficient conventions. Preserve source JSON and label unknowns. Keep source
observations separate from assumed/simulated model values. Import every stored
sensor run, including failed/excluded history. Lens legacy configurations get
separate revision/condition identities; result/PSF content and settings identify
legacy run snapshots (original execution times may be unavailable).
