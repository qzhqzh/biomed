from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Kind = Literal["guideline", "drug", "trial"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CaseFields(StrictModel):
    cancer: str = Field(default="", max_length=100)
    stage: str = Field(default="", max_length=50)
    biomarker: str = Field(default="", max_length=200)
    prior_therapy: str = Field(default="", max_length=300)
    city: str = Field(default="", max_length=100)


class ExtractRequest(StrictModel):
    text: str = Field(min_length=1, max_length=6000)


class SearchRequest(StrictModel):
    fields: CaseFields
    confirmed: Literal[True]


class Evidence(StrictModel):
    id: str
    kind: Kind
    title: str
    summary: str
    cancer: str
    stage: str = ""
    biomarker: str = ""
    city: str = ""
    drug_name: str = ""
    source_title: str
    source_locator: str
    source_version: str
    source_text: str
    updated_at: str
    is_demo: Literal[True]


class IssueCreate(StrictModel):
    title: str = Field(min_length=2, max_length=120)
    category: Literal["extraction", "matching", "source", "interaction", "other"]
    observation: str = Field(min_length=2, max_length=1500)
    expected: str = Field(default="", max_length=1500)
    lesson: str = Field(default="", max_length=1500)
    run_id: UUID | None = None


class IssueUpdate(StrictModel):
    status: Literal["open", "resolved"]
    lesson: str = Field(default="", max_length=1500)

    @field_validator("lesson")
    @classmethod
    def trim_lesson(cls, value: str) -> str:
        return value.strip()
