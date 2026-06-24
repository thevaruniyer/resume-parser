"""
Phase 4: Column layout — schema field path → display header string.

COLUMN_MAP defines the output column set for the Excel workbook.
Swap the VALUES to substitute client-provided column headers; key order
determines column order. The extractor table (_EXTRACTORS) maps each key
to a callable that pulls the value from a record dict.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# COLUMN_MAP: logical_key → display header
# ---------------------------------------------------------------------------

COLUMN_MAP: dict[str, str] = {
    # ---- IDENTITY -----------------------------------------------------------
    "full_name":         "Full Name",
    "first_name":        "First Name",
    "last_name":         "Last Name",
    "date_of_birth":     "Date of Birth",
    "gender":            "Gender",
    "nationality":       "Nationality",
    "marital_status":    "Marital Status",
    "current_city":      "Current City",
    "current_state":     "Current State",
    "current_country":   "Current Country",
    # ---- CONTACT ------------------------------------------------------------
    "primary_email":     "Primary Email",
    "all_emails":        "All Emails",
    "primary_phone":     "Primary Phone",
    "all_phones":        "All Phones",
    "linkedin_url":      "LinkedIn URL",
    "github_url":        "GitHub URL",
    "portfolio_url":     "Portfolio URL",
    # ---- PROFESSIONAL SUMMARY -----------------------------------------------
    "summary_objective": "Summary / Objective",
    # ---- CURRENT ROLE (derived) ---------------------------------------------
    "current_employer":        "Current Employer",
    "current_designation":     "Current Designation",
    "current_employment_type": "Current Employment Type",
    "total_experience_years":  "Total Experience (Years)",
    # ---- WORK EXPERIENCE ----------------------------------------------------
    "work_experience_count":   "# Work Experiences",
    "work_experience_summary": "Work Experience Summary",
    "work_experience_full":    "Work Experience (JSON)",
    "job1_title":              "Most Recent Job Title",
    "job1_company":            "Most Recent Company",
    "job1_start":              "Most Recent Job Start",
    "job1_end":                "Most Recent Job End",
    "job1_duration_months":    "Most Recent Job Duration (Months)",
    "job1_type":               "Most Recent Job Type",
    "job1_location":           "Most Recent Job Location",
    # ---- EDUCATION ----------------------------------------------------------
    "education_count":             "# Education Entries",
    "education_summary":           "Education Summary",
    "highest_degree":              "Highest Degree",
    "highest_degree_institution":  "Highest Degree Institution",
    "highest_degree_year":         "Highest Degree Year",
    "highest_grade":               "Highest Degree Grade",
    "education_full":              "Education (JSON)",
    # ---- QUALIFICATIONS (CA/CMA/CS/ACCA/CPA/bar/medical/any) ---------------
    "qualifications_count":   "# Qualifications",
    "qualification_names":    "Qualification Names",
    "qualification_bodies":   "Qualification Bodies",
    "qualification_statuses": "Qualification Statuses",
    # CA / ICAI specific — null for non-CA candidates
    "icai_membership_number": "ICAI Membership Number",
    "ca_level":               "CA Level",
    "ca_attempts":            "CA Final Attempts",
    # ---- ARTICLESHIP / INTERNSHIPS ------------------------------------------
    "articleship_count":            "# Articleship / Internships",
    "articleship_firm":             "Articleship Firm",
    "articleship_start":            "Articleship Start",
    "articleship_end":              "Articleship End",
    "articleship_duration_months":  "Articleship Duration (Months)",
    "articleship_areas":            "Articleship Areas",
    # ---- SKILLS -------------------------------------------------------------
    "skills_count":      "# Skills",
    "all_skills":        "All Skills",
    "technical_skills":  "Technical Skills",
    "soft_skills":       "Soft Skills",
    # ---- LANGUAGES ----------------------------------------------------------
    "languages_known": "Languages Known",
    # ---- PROJECTS -----------------------------------------------------------
    "projects_count":   "# Projects",
    "projects_summary": "Projects Summary",
    "projects_full":    "Projects (JSON)",
    # ---- ACHIEVEMENTS / EXTRAS ----------------------------------------------
    "achievements":     "Achievements / Awards",
    "publications":     "Publications",
    "extracurriculars": "Extracurriculars",
    # ---- PROVENANCE (always populated) --------------------------------------
    "source_file":        "Source File",
    "file_type":          "File Type",
    "path_taken":         "Extraction Path",
    "parse_timestamp":    "Parse Timestamp",
    "model_used":         "Model Used",
    "overall_confidence": "Overall Confidence",
    "needs_review":       "Needs Review",
    "review_reasons":     "Review Reasons",
    "dedup_key":          "Dedup Key",
    # ---- RAW CATCH-ALL ------------------------------------------------------
    "raw_json": "Raw JSON",
}

COLUMN_HEADERS: list[str] = list(COLUMN_MAP.values())


# ---------------------------------------------------------------------------
# Extractor helpers
# ---------------------------------------------------------------------------

def _g(r: dict, *keys: str, default: Any = None) -> Any:
    obj: Any = r
    for k in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k, default)
    return obj


def _join(items: Any, sep: str = "; ") -> Optional[str]:
    """Join non-empty items; returns None when nothing to join."""
    parts = [str(x).strip() for x in (items or []) if x is not None and str(x).strip()]
    return sep.join(parts) if parts else None


def _loc_str(loc: Any) -> Optional[str]:
    if not isinstance(loc, dict):
        return None
    parts = [p for p in [loc.get("city"), loc.get("state")] if p]
    return ", ".join(parts) if parts else None


def _we_list(r: dict) -> list[dict]:
    return [w for w in (r.get("work_experience") or []) if isinstance(w, dict)]


def _edu_list(r: dict) -> list[dict]:
    return [e for e in (r.get("education") or []) if isinstance(e, dict)]


def _qual_list(r: dict) -> list[dict]:
    return [q for q in (r.get("qualifications") or []) if isinstance(q, dict)]


def _art_list(r: dict) -> list[dict]:
    return [a for a in (r.get("articleship_internships") or []) if isinstance(a, dict)]


def _skill_list(r: dict) -> list[dict]:
    return [s for s in (r.get("skills") or []) if isinstance(s, dict)]


def _current_job(r: dict) -> dict:
    for we in _we_list(r):
        if we.get("is_current"):
            return we
    return {}


_CA_LEVEL_ORDER = {"Foundation": 1, "Intermediate": 2, "Final": 3}


def _ca_quals(r: dict) -> list[dict]:
    return [q for q in _qual_list(r) if q.get("name") == "CA"]


def _highest_ca_qual(r: dict) -> dict:
    caq = _ca_quals(r)
    return max(caq, key=lambda q: _CA_LEVEL_ORDER.get(q.get("level", ""), 0)) if caq else {}


def _edu_institution(r: dict) -> Optional[str]:
    edu = _edu_list(r)
    if not edu:
        return None
    e = edu[0]
    return e.get("institution") or e.get("board_or_university")


def _edu_grade(r: dict) -> Optional[str]:
    edu = _edu_list(r)
    if not edu or not edu[0].get("grade_value"):
        return None
    e = edu[0]
    gtype = e.get("grade_type", "")
    return f"{e['grade_value']} ({gtype})" if gtype else str(e["grade_value"])


def _we_summary(r: dict) -> Optional[str]:
    entries = [
        f"{we.get('designation','?')} @ {we.get('company','?')}"
        f" ({we.get('start_date','?')}–{we.get('end_date') or 'Present'})"
        for we in _we_list(r)
    ]
    return _join(entries, sep=" | ")


def _edu_summary(r: dict) -> Optional[str]:
    entries = [
        f"{edu.get('degree','?')}, "
        f"{edu.get('institution') or edu.get('board_or_university','?')}"
        f" ({edu.get('start_date','?')}–{edu.get('end_date','?')})"
        for edu in _edu_list(r)
    ]
    return _join(entries, sep=" | ")


def _icai_membership(r: dict) -> Optional[str]:
    for q in _qual_list(r):
        if q.get("body") == "ICAI" and q.get("membership_number"):
            return q["membership_number"]
    return None


def _ca_final_attempts(r: dict) -> Optional[int]:
    for q in _ca_quals(r):
        if q.get("level") == "Final":
            return q.get("attempts")
    return None


def _languages_str(r: dict) -> Optional[str]:
    parts = []
    for lang in (r.get("languages_known") or []):
        if not isinstance(lang, dict) or not lang.get("language"):
            continue
        prof = lang.get("proficiency")
        parts.append(f"{lang['language']} ({prof})" if prof else lang["language"])
    return _join(parts, sep="; ")


# ---------------------------------------------------------------------------
# Extractor dispatch table (keyed by COLUMN_MAP keys)
# ---------------------------------------------------------------------------

_EXTRACTORS: dict[str, Callable[[dict], Any]] = {
    # IDENTITY
    "full_name":      lambda r: r.get("full_name"),
    "first_name":     lambda r: r.get("first_name"),
    "last_name":      lambda r: r.get("last_name"),
    "date_of_birth":  lambda r: r.get("date_of_birth"),
    "gender":         lambda r: r.get("gender"),
    "nationality":    lambda r: r.get("nationality"),
    "marital_status": lambda r: r.get("marital_status"),
    "current_city":   lambda r: _g(r, "location", "city"),
    "current_state":  lambda r: _g(r, "location", "state"),
    "current_country": lambda r: _g(r, "location", "country"),
    # CONTACT
    "primary_email":  lambda r: (_g(r, "emails") or [None])[0],
    "all_emails":     lambda r: _join(r.get("emails")),
    "primary_phone":  lambda r: (_g(r, "phones") or [None])[0],
    "all_phones":     lambda r: _join(r.get("phones")),
    "linkedin_url":   lambda r: _g(r, "links", "linkedin"),
    "github_url":     lambda r: _g(r, "links", "github"),
    "portfolio_url":  lambda r: _g(r, "links", "portfolio"),
    # SUMMARY
    "summary_objective": lambda r: r.get("summary_objective"),
    # CURRENT ROLE
    "current_employer":        lambda r: _g(r, "derived", "current_employer"),
    "current_designation":     lambda r: _g(r, "derived", "current_designation"),
    "current_employment_type": lambda r: _current_job(r).get("employment_type"),
    "total_experience_years":  lambda r: _g(r, "derived", "total_experience_years"),
    # WORK EXPERIENCE
    "work_experience_count":   lambda r: len(_we_list(r)),
    "work_experience_summary": _we_summary,
    "work_experience_full": lambda r: (
        json.dumps(_we_list(r), ensure_ascii=False, separators=(",", ":"))
        if _we_list(r) else None
    ),
    "job1_title":           lambda r: _we_list(r)[0].get("designation") if _we_list(r) else None,
    "job1_company":         lambda r: _we_list(r)[0].get("company") if _we_list(r) else None,
    "job1_start":           lambda r: _we_list(r)[0].get("start_date") if _we_list(r) else None,
    "job1_end":             lambda r: _we_list(r)[0].get("end_date") if _we_list(r) else None,
    "job1_duration_months": lambda r: _we_list(r)[0].get("duration_months") if _we_list(r) else None,
    "job1_type":            lambda r: _we_list(r)[0].get("employment_type") if _we_list(r) else None,
    "job1_location":        lambda r: _loc_str(_we_list(r)[0].get("location")) if _we_list(r) else None,
    # EDUCATION
    "education_count":            lambda r: len(_edu_list(r)),
    "education_summary":          _edu_summary,
    "highest_degree":             lambda r: _edu_list(r)[0].get("degree") if _edu_list(r) else None,
    "highest_degree_institution": _edu_institution,
    "highest_degree_year":        lambda r: _edu_list(r)[0].get("end_date") if _edu_list(r) else None,
    "highest_grade":              _edu_grade,
    "education_full": lambda r: (
        json.dumps(_edu_list(r), ensure_ascii=False, separators=(",", ":"))
        if _edu_list(r) else None
    ),
    # QUALIFICATIONS
    "qualifications_count":   lambda r: len(_qual_list(r)),
    "qualification_names":    lambda r: _join([q.get("name") for q in _qual_list(r)]),
    "qualification_bodies":   lambda r: _join([q.get("body") for q in _qual_list(r)]),
    "qualification_statuses": lambda r: _join([q.get("status") for q in _qual_list(r)]),
    "icai_membership_number": _icai_membership,
    "ca_level":               lambda r: _highest_ca_qual(r).get("level"),
    "ca_attempts":            _ca_final_attempts,
    # ARTICLESHIP
    "articleship_count":           lambda r: len(_art_list(r)),
    "articleship_firm":            lambda r: _art_list(r)[0].get("firm_or_org") if _art_list(r) else None,
    "articleship_start":           lambda r: _art_list(r)[0].get("start_date") if _art_list(r) else None,
    "articleship_end":             lambda r: _art_list(r)[0].get("end_date") if _art_list(r) else None,
    "articleship_duration_months": lambda r: _art_list(r)[0].get("duration_months") if _art_list(r) else None,
    "articleship_areas": lambda r: (
        _join(_art_list(r)[0].get("areas")) if _art_list(r) else None
    ),
    # SKILLS
    "skills_count": lambda r: len(_skill_list(r)),
    "all_skills":   lambda r: _join([s.get("name") for s in _skill_list(r)]),
    "technical_skills": lambda r: _join([
        s.get("name") for s in _skill_list(r)
        if (s.get("category") or "").lower() in ("technical", "tech")
    ]),
    "soft_skills": lambda r: _join([
        s.get("name") for s in _skill_list(r)
        if (s.get("category") or "").lower() in ("soft", "soft skill", "soft skills")
    ]),
    # LANGUAGES
    "languages_known": _languages_str,
    # PROJECTS
    "projects_count": lambda r: len(r.get("projects") or []),
    "projects_summary": lambda r: _join(
        [p.get("title") for p in (r.get("projects") or []) if isinstance(p, dict)],
        sep=" | ",
    ),
    "projects_full": lambda r: (
        json.dumps([p for p in (r.get("projects") or []) if isinstance(p, dict)],
                   ensure_ascii=False, separators=(",", ":"))
        if r.get("projects") else None
    ),
    # ACHIEVEMENTS
    "achievements":     lambda r: _join(r.get("achievements_awards")),
    "publications":     lambda r: _join(r.get("publications")),
    "extracurriculars": lambda r: _join(r.get("extracurriculars")),
    # PROVENANCE
    "source_file":        lambda r: _g(r, "meta", "source_file"),
    "file_type":          lambda r: _g(r, "meta", "file_type"),
    "path_taken":         lambda r: _g(r, "meta", "path_taken"),
    "parse_timestamp":    lambda r: _g(r, "meta", "parse_timestamp"),
    "model_used":         lambda r: _g(r, "meta", "model_used"),
    "overall_confidence": lambda r: _g(r, "meta", "overall_confidence"),
    "needs_review":       lambda r: _g(r, "meta", "needs_review"),
    "review_reasons":     lambda r: _join(_g(r, "meta", "review_reasons")),
    "dedup_key":          lambda r: _g(r, "meta", "dedup_key"),
    # RAW
    "raw_json": lambda r: json.dumps(r, ensure_ascii=False, separators=(",", ":")),
}


def record_to_row(record: dict) -> list[Any]:
    """Return a flat list of cell values in COLUMN_MAP order."""
    return [_EXTRACTORS[key](record) for key in COLUMN_MAP]
