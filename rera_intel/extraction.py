from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


MVC_DATE_RE = re.compile(r"/Date\((?P<timestamp>-?\d+)(?:[+-]\d+)?\)/")
EMPTY_MARKERS = {"", "na", "n/a", "none", "null", "nan"}


def safe_filename(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[\\/*?:"<>|]', "_", text)
    return text[:150]


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in EMPTY_MARKERS:
        return None
    return text


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if isinstance(value, str):
            cleaned = clean_text(value)
            if cleaned is not None:
                return cleaned
        elif value is not None:
            return value
    return None


def parse_date_value(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()

    text = clean_text(value)
    if text is None:
        return None

    match = MVC_DATE_RE.fullmatch(text)
    if match:
        timestamp = int(match.group("timestamp"))
        return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).date()

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass

    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    return None


def parse_datetime_value(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    text = clean_text(value)
    if text is None:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_promoter_name(promoter_details: dict[str, Any], csv_row: dict[str, Any] | None) -> str | None:
    csv_row = csv_row or {}
    full_name = " ".join(
        part
        for part in [
            clean_text(csv_row.get("FIRSTNAME")),
            clean_text(csv_row.get("MIDDLENAME")),
            clean_text(csv_row.get("LASTNAME")),
        ]
        if part
    )
    return first_non_empty(
        promoter_details.get("OrgName"),
        promoter_details.get("FirstName"),
        promoter_details.get("LastName"),
        full_name,
        csv_row.get("PromoterName"),
        csv_row.get("ORGNAME"),
    )


def extract_project_fields(
    raw_json: dict[str, Any] | None,
    csv_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    csv_row = csv_row or {}
    raw_json = raw_json or {}

    basic = raw_json.get("GetProjectBasic")
    if not isinstance(basic, dict):
        basic = {}

    promoter_details = raw_json.get("PromoterDetails")
    if not isinstance(promoter_details, dict):
        promoter_details = {}

    approved_on = parse_date_value(
        first_non_empty(basic.get("ApprovedOn"), csv_row.get("APPROVEDON"))
    )
    original_completion_date = parse_date_value(basic.get("DateOfComplation"))
    revised_completion_date = parse_date_value(
        first_non_empty(
            basic.get("RevisedDateOfComplation"),
            csv_row.get("RevisedDateOfComplation"),
        )
    )
    actual_commencement_date = parse_date_value(basic.get("ActualCommencementDate"))
    csv_updated_on = parse_datetime_value(csv_row.get("UpdatedOn"))

    project_status = first_non_empty(
        csv_row.get("AppStatus"),
        csv_row.get("ProjectStatus"),
        csv_row.get("Status"),
        basic.get("ProjectStatus"),
        basic.get("PStatus"),
    )

    fields = {
        "registration_no": first_non_empty(
            basic.get("RegistrationNo"),
            csv_row.get("REGISTRATIONNO"),
        ),
        "project_name": first_non_empty(
            basic.get("Name"),
            csv_row.get("ProjectName"),
        ),
        "district_name": first_non_empty(
            basic.get("DistrictName"),
            csv_row.get("DistrictName"),
        ),
        "promoter_name": build_promoter_name(promoter_details, csv_row),
        "project_type": first_non_empty(
            basic.get("ProjectTypeName"),
            csv_row.get("ProjectTypeName"),
            csv_row.get("ProjectType"),
        ),
        "application_no": first_non_empty(
            basic.get("ApplicationNo"),
            raw_json.get("ApplicationNo"),
            csv_row.get("ApplicationNo"),
        ),
        "certificate_no": clean_text(csv_row.get("CertificateNo")),
        "project_status": str(project_status) if project_status is not None else None,
        "approved_on": approved_on,
        "approved_year": approved_on.year if approved_on else None,
        "original_completion_date": original_completion_date,
        "revised_completion_date": revised_completion_date,
        "actual_commencement_date": actual_commencement_date,
        "tahsil_name": first_non_empty(
            basic.get("TahsilName"),
            csv_row.get("Taluka"),
        ),
        "village_name": clean_text(basic.get("VillageName")),
        "plot_no": first_non_empty(
            basic.get("ProjectPlotNo"),
            basic.get("PlotNo"),
        ),
        "area_sqm": float_or_none(basic.get("Area")),
        "phase_area_sqm": float_or_none(basic.get("PhaseArea")),
        "saleable_area_sqm": float_or_none(
            first_non_empty(basic.get("SaleableArea"), basic.get("BuiltUpAreaFSI"))
        ),
        "total_building_count": int_or_none(basic.get("TotalBuildingCount")),
        "sanctioned_building_count": int_or_none(
            basic.get("SanctionedbuildingCount")
        ),
        "not_sanctioned_building_count": int_or_none(
            basic.get("NotSanctionedbuildingCount")
        ),
        "csv_updated_on": csv_updated_on,
    }
    return fields


def serialize_project_fields(fields: dict[str, Any]) -> dict[str, Any]:
    serializable: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, (datetime, date)):
            serializable[key] = value.isoformat()
        else:
            serializable[key] = value
    return serializable


def canonical_json(raw_json: dict[str, Any]) -> str:
    return json.dumps(
        raw_json,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(raw_json: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(raw_json).encode("utf-8")).hexdigest()


def flatten_json(value: Any, prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}

    if isinstance(value, dict):
        if not value and prefix:
            flattened[prefix] = {}
        for key in sorted(value.keys()):
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(flatten_json(value[key], next_prefix))
        return flattened

    if isinstance(value, list):
        if not value and prefix:
            flattened[prefix] = []
        for index, item in enumerate(value):
            next_prefix = f"{prefix}[{index}]"
            flattened.update(flatten_json(item, next_prefix))
        return flattened

    if prefix:
        flattened[prefix] = value
    return flattened


def diff_json_documents(
    old_json: dict[str, Any] | None,
    new_json: dict[str, Any],
) -> list[dict[str, Any]]:
    old_flat = flatten_json(old_json or {})
    new_flat = flatten_json(new_json)
    changes: list[dict[str, Any]] = []
    missing = object()

    for path in sorted(set(old_flat) | set(new_flat)):
        old_value = old_flat.get(path, missing)
        new_value = new_flat.get(path, missing)
        if old_value == new_value:
            continue

        if old_value is missing:
            change_type = "added"
            old_payload = None
            new_payload = new_value
        elif new_value is missing:
            change_type = "removed"
            old_payload = old_value
            new_payload = None
        else:
            change_type = "modified"
            old_payload = old_value
            new_payload = new_value

        changes.append(
            {
                "field_path": path,
                "change_type": change_type,
                "old_value": old_payload,
                "new_value": new_payload,
            }
        )

    return changes


def derive_encrypted_project_id(
    json_path: Path,
    raw_json: dict[str, Any],
    known_ids: list[str] | None = None,
) -> str | None:
    stem = json_path.stem

    if known_ids:
        matches = [
            encrypted_id
            for encrypted_id in known_ids
            if stem == encrypted_id or stem.endswith(f"_{encrypted_id}")
        ]
        if matches:
            return max(matches, key=len)

    fields = extract_project_fields(raw_json)
    registration_no = fields.get("registration_no")
    project_name = fields.get("project_name")
    if registration_no and project_name:
        prefix = f"{safe_filename(registration_no)}_{safe_filename(project_name)}_"
        if stem.startswith(prefix):
            return stem[len(prefix) :]

    return None

