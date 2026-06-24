"""
Pydantic v2 resume record schema.

Design rules (from CLAUDE.md):
- Capture ALL list entries — never just the latest.
- Tuned for Indian CA applicants: qualifications + articleship blocks are first-class.
- `meta` provenance block is mandatory and always present.
- `derived` fields are computed downstream, never extracted directly.
- Ask before inventing columns; extend only when instructed.
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class Location(BaseModel):
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None


class Links(BaseModel):
    linkedin: Optional[str] = None
    github: Optional[str] = None
    portfolio: Optional[str] = None
    other: list[str] = Field(default_factory=list)


class Language(BaseModel):
    language: str
    proficiency: Optional[str] = None  # native|fluent|working|basic


class Education(BaseModel):
    degree: Optional[str] = None
    specialization: Optional[str] = None
    institution: Optional[str] = None
    board_or_university: Optional[str] = None
    location: Optional[Location] = None
    start_date: Optional[str] = None   # YYYY-MM
    end_date: Optional[str] = None     # YYYY-MM; null if pursuing
    duration_years: Optional[float] = None   # derived from dates
    status: Optional[Literal["completed", "pursuing", "discontinued"]] = None
    grade_type: Optional[Literal["CGPA", "percentage", "grade"]] = None
    grade_value: Optional[str] = None


class Qualification(BaseModel):
    """Professional qualifications — critical for CA/CMA/CS applicants."""
    name: Optional[str] = None          # CA, CMA, CS, ACCA, CPA, CFA …
    body: Optional[str] = None          # ICAI, ICSI, ICMAI, AICPA …
    level: Optional[str] = None         # Foundation | Intermediate | Final
    membership_number: Optional[str] = None  # ICAI membership no. post-qualification
    attempts: Optional[int] = None      # number of attempts taken
    date_cleared: Optional[str] = None  # YYYY-MM
    status: Optional[Literal["cleared", "pursuing", "pending_result"]] = None


class WorkExperience(BaseModel):
    company: Optional[str] = None
    designation: Optional[str] = None
    employment_type: Optional[
        Literal["full_time", "part_time", "contract", "intern"]
    ] = None
    location: Optional[Location] = None
    start_date: Optional[str] = None   # YYYY-MM
    end_date: Optional[str] = None     # YYYY-MM; null if current
    is_current: bool = False
    duration_months: Optional[int] = None  # derived
    responsibilities: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    tools_technologies: list[str] = Field(default_factory=list)
    industry: Optional[str] = None


class ArticleshipInternship(BaseModel):
    """CA articleship / internship — India-specific training period."""
    firm_or_org: Optional[str] = None
    role: Optional[str] = None
    start_date: Optional[str] = None   # YYYY-MM
    end_date: Optional[str] = None     # YYYY-MM
    duration_months: Optional[int] = None  # derived
    areas: list[str] = Field(default_factory=list)
    # e.g. audit, direct tax, GST, transfer pricing, statutory audit,
    # internal audit, company law, bank audit …


class Skill(BaseModel):
    name: str
    category: Optional[str] = None     # e.g. technical, soft, domain
    proficiency: Optional[str] = None  # beginner|intermediate|advanced|expert


class Project(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    role: Optional[str] = None
    tools_technologies: list[str] = Field(default_factory=list)
    duration: Optional[str] = None     # free-text, e.g. "3 months"


class DerivedFields(BaseModel):
    """Computed by normalize layer — never extracted from resume text."""
    total_experience_years: Optional[float] = None
    current_employer: Optional[str] = None
    current_designation: Optional[str] = None
    highest_qualification: Optional[str] = None  # e.g. "CA Final"
    key_skill_tags: list[str] = Field(default_factory=list)


class MetaBlock(BaseModel):
    """Provenance — always present, never skip."""
    source_file: str
    source_path: str
    file_type: str   # txt | pdf | docx | doc | jpg | png | …
    parse_timestamp: str  # ISO 8601
    model_used: Optional[str] = None
    path_taken: Optional[Literal["text", "vision", "hybrid"]] = None
    overall_confidence: Optional[float] = None  # 0–1
    field_confidences: dict[str, float] = Field(default_factory=dict)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)

    @field_validator("overall_confidence")
    @classmethod
    def confidence_range(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("overall_confidence must be between 0 and 1")
        return v


# ---------------------------------------------------------------------------
# Root record
# ---------------------------------------------------------------------------

class ResumeRecord(BaseModel):
    """
    One parsed resume.  Every list field captures ALL entries found —
    never just the most recent.  Fields absent from the resume are null,
    not invented.
    """

    # --- Identity / contact ---
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    location: Optional[Location] = None
    date_of_birth: Optional[str] = None    # YYYY-MM-DD
    gender: Optional[str] = None
    nationality: Optional[str] = None
    marital_status: Optional[str] = None
    has_photo: bool = False                 # flag only — image never stored
    links: Optional[Links] = None
    languages_known: list[Language] = Field(default_factory=list)
    summary_objective: Optional[str] = None

    # --- Education (ALL entries) ---
    education: list[Education] = Field(default_factory=list)

    # --- Professional qualifications (ALL entries — CA/CMA/CS critical) ---
    qualifications: list[Qualification] = Field(default_factory=list)

    # --- Work experience (ALL entries) ---
    work_experience: list[WorkExperience] = Field(default_factory=list)

    # --- Articleship / internships (India CA-specific) ---
    articleship_internships: list[ArticleshipInternship] = Field(default_factory=list)

    # --- Extras ---
    skills: list[Skill] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    achievements_awards: list[str] = Field(default_factory=list)
    publications: list[str] = Field(default_factory=list)
    extracurriculars: list[str] = Field(default_factory=list)

    # --- Derived (computed, not extracted) ---
    derived: Optional[DerivedFields] = None

    # --- Provenance (mandatory) ---
    meta: MetaBlock
