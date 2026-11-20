"""Records of HTTP fetches."""

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator


class Capture(BaseModel):
    """One HTTP attempt.

    not_modified_of names an earlier capture this one re-confirms: either the server
    answered 304, or the body matched it after normalization and was not stored
    again. Either way content_hash points at the earlier body.
    """

    model_config = ConfigDict(frozen=True)

    capture_id: UUID
    run_id: UUID
    url: str
    request_headers: dict[str, str] = {}
    requested_at: AwareDatetime
    http_status: int
    content_hash: str | None = None
    normalized_hash: str | None = None
    not_modified_of: UUID | None = None
    media_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> "Capture":
        if self.http_status == 304 and self.not_modified_of is None:
            raise ValueError("A 304 capture must reference the capture it confirms")
        if self.http_status == 200 and self.content_hash is None:
            raise ValueError("A 200 capture must have a content hash")
        return self
