"""Pointers from conclusions back to observations."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EvidenceRef(BaseModel):
    """An observation, optionally narrowed to one history row."""

    model_config = ConfigDict(frozen=True)

    observation_id: UUID
    row_index: int | None = None
