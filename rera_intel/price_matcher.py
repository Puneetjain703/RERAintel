from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


SOURCE_QUALITY_SCORES = {
    "99acres": 0.10,
    "magicbricks": 0.10,
    "housing": 0.10,
    "squareyards": 0.08,
    "makaan": 0.08,
    "commonfloor": 0.07,
}


def normalize_text(value: Any) -> str:
    text = str(value or "").lower()
    cleaned = []
    previous_space = False
    for char in text:
        if char.isalnum():
            cleaned.append(char)
            previous_space = False
        else:
            if not previous_space:
                cleaned.append(" ")
            previous_space = True
    return " ".join("".join(cleaned).split())


def tokenize(value: Any) -> set[str]:
    return {token for token in normalize_text(value).split() if len(token) >= 3}


def detect_source_name(url: str | None) -> str:
    if not url:
        return "Unknown"
    hostname = urlparse(url).netloc.lower().replace("www.", "")
    if "99acres" in hostname:
        return "99acres"
    if "magicbricks" in hostname:
        return "Magicbricks"
    if "housing" in hostname:
        return "Housing"
    if "squareyards" in hostname:
        return "Squareyards"
    if "makaan" in hostname:
        return "Makaan"
    if hostname:
        return hostname
    return "Unknown"


def project_type_tokens(project_type: str | None) -> set[str]:
    normalized = normalize_text(project_type)
    mapped: set[str] = set()
    if "plotted" in normalized:
        mapped.update({"plot", "plots", "plotted"})
    if "residential" in normalized:
        mapped.update({"residential", "residency", "apartment", "flat"})
    if "commercial" in normalized:
        mapped.update({"commercial", "office", "retail", "shop"})
    return mapped | tokenize(project_type)


def score_price_candidate(
    *,
    project: dict[str, Any],
    result_title: str,
    result_snippet: str,
    source_url: str | None,
) -> dict[str, Any]:
    haystack = normalize_text(" ".join(filter(None, [result_title, result_snippet, source_url or ""])))
    haystack_tokens = tokenize(haystack)
    reasons: list[str] = []
    score = 0.0

    project_name = project.get("project_name") or ""
    project_name_norm = normalize_text(project_name)
    project_name_tokens = tokenize(project_name)
    if project_name_norm and project_name_norm in haystack:
        score += 0.35
        reasons.append("Exact project name match")
    else:
        overlap = len(project_name_tokens & haystack_tokens)
        if overlap >= max(2, len(project_name_tokens) // 2) and project_name_tokens:
            score += 0.18
            reasons.append("Strong project name token overlap")

    promoter_name = project.get("promoter_name") or ""
    promoter_name_norm = normalize_text(promoter_name)
    promoter_tokens = tokenize(promoter_name)
    if promoter_name_norm and promoter_name_norm in haystack:
        score += 0.15
        reasons.append("Promoter name match")
    else:
        overlap = len(promoter_tokens & haystack_tokens)
        if overlap >= 1 and promoter_tokens:
            score += 0.08
            reasons.append("Promoter token overlap")

    location_fields = [
        project.get("district_name"),
        project.get("tahsil_name"),
        project.get("village_name"),
    ]
    location_tokens = set().union(*(tokenize(value) for value in location_fields if value))
    if location_tokens & haystack_tokens:
        score += 0.10
        reasons.append("District or location match")

    registration_no = normalize_text(project.get("registration_no") or "")
    if registration_no and registration_no in haystack:
        score += 0.25
        reasons.append("RERA registration match")

    source_name = detect_source_name(source_url)
    source_key = normalize_text(source_name).replace(" ", "")
    source_quality = SOURCE_QUALITY_SCORES.get(source_key, 0.03)
    score += source_quality
    reasons.append(f"Source quality: {source_name}")

    type_tokens = project_type_tokens(project.get("project_type"))
    if type_tokens & haystack_tokens:
        score += 0.05
        reasons.append("Project type match")

    score = max(0.0, min(score, 1.0))
    return {
        "confidence_score": round(score, 4),
        "match_reason": "; ".join(reasons) if reasons else "Basic source match only",
        "source": source_name,
    }
