"""Files a spot check reads and writes: the sample manifest, verdicts, the ledger."""

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from beacon.domain.evaluation import ComplianceLabel, RequirementStatus


class Verdict(StrEnum):
    """An auditor's judgment of one tenure's evaluation."""

    AGREE = "agree"
    DISAGREE = "disagree"
    UNCERTAIN = "uncertain"


class FindingCategory(StrEnum):
    """The pipeline layer a finding implicates."""

    EXTRACTION = "extraction"
    TENURE = "tenure"
    RULE_LOGIC = "rule_logic"
    RULE_AMBIGUITY = "rule_ambiguity"
    DATA_GAP = "data_gap"
    PRESENTATION = "presentation"


class ResultSummary(BaseModel):
    """One requirement outcome, as recorded in a manifest."""

    model_config = ConfigDict(frozen=True)

    requirement_id: str
    status: RequirementStatus
    reason: str | None = None


class ManifestItem(BaseModel):
    """One sampled tenure: what the current code concluded, and what was stored."""

    model_config = ConfigDict(frozen=True)

    item: int
    session: int
    bill_id: str
    committee_id: str
    tenure_id: str
    signature: str
    label: ComplianceLabel
    results: tuple[ResultSummary, ...]
    input_fingerprint: str
    stored_evaluation_id: UUID
    stored_ruleset_version: str
    stored_label: ComplianceLabel
    dossier: str
    key: str


class Manifest(BaseModel):
    """A spot-check sample: how it was drawn and what it contains."""

    model_config = ConfigDict(frozen=True)

    sample_id: str
    seed: int
    as_of: AwareDatetime
    code_version: str
    ruleset_id: str
    ruleset_version: str
    committees: tuple[str, ...]
    label_filter: ComplianceLabel | None = None
    frame_size: int
    items: tuple[ManifestItem, ...]

    def get(self, item: int) -> ManifestItem:
        """The item with this number. Raises KeyError if absent."""
        for entry in self.items:
            if entry.item == item:
                return entry
        raise KeyError(item)


class Finding(BaseModel):
    """One problem an auditor found, tied to a requirement when it concerns one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: FindingCategory
    requirement_id: str | None = None
    note: str = Field(min_length=1)


class ItemVerdict(BaseModel):
    """An auditor's verdict on one manifest item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item: int
    verdict: Verdict
    expected_label: ComplianceLabel | None = None
    findings: tuple[Finding, ...] = ()
    note: str = ""

    @model_validator(mode="after")
    def _findings_iff_not_agree(self) -> "ItemVerdict":
        if (self.verdict == Verdict.AGREE) == bool(self.findings):
            raise ValueError("findings are required unless the verdict is agree")
        return self


class VerdictSheet(BaseModel):
    """verdicts.json: the auditor's verdicts on a sample."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    auditor: str = Field(min_length=1)
    items: tuple[ItemVerdict, ...]

    @model_validator(mode="after")
    def _unique_items(self) -> "VerdictSheet":
        numbers = [v.item for v in self.items]
        if len(numbers) != len(set(numbers)):
            raise ValueError("each item may have only one verdict")
        return self


class LedgerEntry(BaseModel):
    """One audited tenure, appended to the ledger."""

    model_config = ConfigDict(frozen=True)

    audited_at: AwareDatetime
    auditor: str
    sample_id: str
    seed: int
    session: int
    bill_id: str
    committee_id: str
    tenure_id: str
    ruleset_id: str
    ruleset_version: str
    code_version: str
    input_fingerprint: str
    as_of: AwareDatetime
    signature: str
    label: ComplianceLabel
    verdict: Verdict
    expected_label: ComplianceLabel | None = None
    findings: tuple[Finding, ...] = ()
    note: str = ""
