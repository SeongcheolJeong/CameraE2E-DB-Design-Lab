"""Stored radiance can change camera response only on its original ray grid."""

from pydantic import Field

from .contracts import Contract
from .scene_contracts import ProjectionSpec


class SceneProjectionCreate(Contract):
    name: str = Field(min_length=1, max_length=200)
    source_render_id: str
    assembly_id: str
    projection: ProjectionSpec
    force: bool = False


class SceneProjectionSubmit(Contract):
    draft: SceneProjectionCreate
    preview_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
