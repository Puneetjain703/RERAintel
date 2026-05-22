from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from openai import OpenAI

from .config import ROOT_DIR, get_settings
from .db import get_connection
from .roi_calculator import calculate_roi_metrics


DEFAULT_AI_CHAT_MODEL = "gpt-5.4-mini"
AI_CHAT_MAX_TOOL_STEPS = 6
AI_CHAT_SQL_MAX_ROWS = 100
AI_CHAT_SQL_STATEMENT_TIMEOUT_MS = 12000
AI_CHAT_MAX_HISTORY_TURNS = 5
AI_CHAT_LOG_PATH = ROOT_DIR / "logs" / "ai_chat_history.jsonl"

AI_CHAT_ALLOWED_TABLES = {
    "rera_projects",
    "rera_project_changes",
    "rera_project_snapshots",
    "project_market_prices",
    "project_price_candidates",
    "project_roi_cases",
}

AI_CHAT_DISALLOWED_SQL_PATTERNS = [
    r"\binsert\b",
    r"\bupdate\b",
    r"\bdelete\b",
    r"\bdrop\b",
    r"\balter\b",
    r"\btruncate\b",
    r"\bcreate\b",
    r"\bgrant\b",
    r"\brevoke\b",
    r"\bcopy\b",
    r"\bcall\b",
    r"\bdo\b",
    r"\bexecute\b",
    r"\bvacuum\b",
    r"\banalyze\b",
    r"\breindex\b",
    r"\bcluster\b",
    r"\brefresh\b",
    r"\battach\b",
    r"\bdetach\b",
    r"\blisten\b",
    r"\bnotify\b",
    r"\bunlisten\b",
    r"\bpg_catalog\b",
    r"\binformation_schema\b",
    r"\bcurrent_setting\b",
    r"\bcurrent_user\b",
    r"\bsession_user\b",
    r"\bversion\b",
    r"\bshow\b",
    r"\bexplain\b",
    r"\bset\b",
    r"\bprepare\b",
    r"\bexecute\b",
    r"\bdblink\b",
    r"\blo_import\b",
    r"\blo_export\b",
    r"\bpg_[a-z0-9_]+\s*\(",
]

AI_CHAT_ALLOWED_SQL_FUNCTIONS = {
    "abs",
    "age",
    "array_agg",
    "avg",
    "coalesce",
    "concat",
    "count",
    "date_trunc",
    "extract",
    "greatest",
    "jsonb_array_length",
    "jsonb_extract_path",
    "jsonb_extract_path_text",
    "jsonb_typeof",
    "least",
    "left",
    "length",
    "lower",
    "max",
    "min",
    "nullif",
    "position",
    "regexp_replace",
    "replace",
    "right",
    "round",
    "split_part",
    "string_agg",
    "sum",
    "to_char",
    "to_date",
    "trim",
    "upper",
}

AI_CHAT_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ai_chat_logs (
    id BIGSERIAL PRIMARY KEY,
    asked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode TEXT,
    model TEXT,
    question TEXT NOT NULL,
    answer TEXT,
    tool_trace JSONB,
    sql_queries JSONB,
    sources JSONB,
    error_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_ai_chat_logs_asked_at
    ON ai_chat_logs (asked_at DESC);
"""

SCHEMA_PURPOSE_HINTS: dict[str, dict[str, str]] = {
    "project_roi_cases": {
        "id": "Internal row identifier for the saved ROI case.",
        "project_id": "Foreign key back to the project in rera_projects.",
        "encrypted_project_id": "Stable project identifier from the source system.",
        "scenario_name": "User label for the ROI scenario.",
        "purchase_price": "Base acquisition price assumed for the investment.",
        "stamp_duty": "Stamp duty cost included in total investment.",
        "registration": "Registration charge included in total investment.",
        "brokerage": "Brokerage cost included in total investment.",
        "other_cost": "Any additional acquisition or carrying cost.",
        "expected_sale_price": "Expected exit value used for ROI estimation.",
        "holding_period_months": "Investment duration used for annualization.",
        "total_investment": "Computed all-in invested amount.",
        "net_profit": "Expected profit after all listed costs.",
        "roi_pct": "Simple ROI percentage on total investment.",
        "annualized_roi_pct": "Annualized return percentage for the holding period.",
        "created_at": "When the ROI case was saved.",
        "updated_at": "When the ROI case was last updated.",
    }
}


def get_ai_chat_model() -> str:
    return (
        os.getenv("OPENAI_CHAT_MODEL", "").strip()
        or os.getenv("OPENAI_RESEARCH_MODEL", "").strip()
        or os.getenv("OPENAI_DB_CHAT_MODEL", "").strip()
        or DEFAULT_AI_CHAT_MODEL
    )


def get_openai_client() -> OpenAI:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in local env or Streamlit secrets.")
    return OpenAI(api_key=settings.openai_api_key)


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def preserve_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def response_to_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if hasattr(response, "to_dict"):
        return response.to_dict()
    return {"output_text": str(response)}


def extract_text_and_sources(response: Any) -> tuple[str, list[dict[str, str]], bool]:
    data = response_to_dict(response)
    output_text = preserve_text(data.get("output_text"))
    chunks: list[str] = [output_text] if output_text else []
    sources: list[dict[str, str]] = []
    used_web = False

    for output_item in data.get("output", []) or []:
        if not isinstance(output_item, dict):
            continue
        item_type = str(output_item.get("type") or "")
        if "web_search" in item_type:
            used_web = True
        for content_item in output_item.get("content", []) or []:
            if not isinstance(content_item, dict):
                continue
            text = preserve_text(content_item.get("text"))
            if text and not chunks:
                chunks.append(text)
            for annotation in content_item.get("annotations", []) or []:
                if not isinstance(annotation, dict):
                    continue
                url = clean_text(annotation.get("url"))
                title = clean_text(annotation.get("title")) or url
                if url:
                    sources.append({"title": title, "url": url})

    for source in data.get("sources", []) or []:
        if not isinstance(source, dict):
            continue
        url = clean_text(source.get("url") or source.get("uri"))
        title = clean_text(source.get("title") or source.get("name")) or url
        if url:
            sources.append({"title": title, "url": url})

    deduped: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for source in sources:
        if source["url"] in seen_urls:
            continue
        seen_urls.add(source["url"])
        deduped.append(source)

    final_text = "\n".join(chunk for chunk in chunks if chunk).strip()
    if not final_text:
        final_text = json.dumps(data, ensure_ascii=False, indent=2)[:6000]
    return final_text, deduped, used_web


@lru_cache(maxsize=2)
def load_schema_metadata(database_url: str) -> dict[str, Any]:
    with get_connection(database_url) as connection:
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
            tables = [row["table_name"] for row in cursor.fetchall()]
            tables = [table for table in tables if table in AI_CHAT_ALLOWED_TABLES]
            cursor.execute(
                """
                SELECT table_name, column_name, data_type, ordinal_position
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = ANY(%s)
                ORDER BY table_name, ordinal_position
                """,
                (tables,),
            )
            column_rows = cursor.fetchall()

    metadata: dict[str, Any] = {"tables": {}}
    for table_name in tables:
        metadata["tables"][table_name] = {"columns": []}

    for row in column_rows:
        table_name = row["table_name"]
        column_name = row["column_name"]
        data_type = row["data_type"]
        metadata["tables"][table_name]["columns"].append(
            {
                "column_name": column_name,
                "data_type": data_type,
                "purpose_hint": guess_column_purpose(table_name, column_name, data_type),
            }
        )

    return metadata


def guess_column_purpose(table_name: str, column_name: str, data_type: str) -> str:
    explicit = SCHEMA_PURPOSE_HINTS.get(table_name, {}).get(column_name)
    if explicit:
        return explicit
    if column_name == "id":
        return "Internal row identifier."
    if column_name.endswith("_id"):
        return "Identifier linking this row to another entity."
    if column_name.endswith("_at"):
        return "Timestamp for when this event or record was created or updated."
    if column_name.endswith("_date") or column_name.endswith("_on"):
        return "Date field for the relevant project event."
    if "price" in column_name:
        return "Pricing field used for market or ROI analysis."
    if "area" in column_name:
        return "Area measure for project sizing or inventory analysis."
    if "json" in column_name:
        return "Raw or semi-structured project data."
    if column_name.startswith("is_"):
        return "Boolean flag."
    if column_name in {"district_name", "tahsil_name", "village_name"}:
        return "Location field used to place the project geographically."
    if column_name == "project_type":
        return "Project category such as plotted, group housing, or commercial."
    if column_name == "project_status":
        return "Current project status from the source data."
    if column_name == "approved_year":
        return "Approval year often used as a launch-time proxy."
    if column_name == "promoter_name":
        return "Developer or promoter name."
    if data_type in {"json", "jsonb"}:
        return "Structured JSON payload."
    return "Database field available for analysis."


def get_schema_summary() -> str:
    settings = get_settings()
    metadata = load_schema_metadata(settings.database_url)
    lines: list[str] = []
    for table_name, table_info in metadata["tables"].items():
        lines.append(f"Table: {table_name}")
        columns = table_info.get("columns") or []
        lines.append(
            "Columns: "
            + ", ".join(f"{column['column_name']} ({column['data_type']})" for column in columns)
        )
    lines.append(
        "Notes: rera_projects is the core project table. "
        "Use village_name first, then tahsil_name, then district_name as an approximate micro-market proxy. "
        "Use approved_year for launch timing, raw_json for detailed project sections, "
        "project_market_prices/project_price_candidates for market evidence, "
        "project_roi_cases for ROI scenarios, and rera_project_changes for detected updates."
    )
    return "\n".join(lines)


def extract_known_table_name(question: str) -> str | None:
    lowered = clean_text(question).casefold()
    for table_name in sorted(AI_CHAT_ALLOWED_TABLES, key=len, reverse=True):
        if table_name.casefold() in lowered:
            return table_name
    return None


def question_looks_like_schema_question(question: str) -> bool:
    lowered = clean_text(question).casefold()
    schema_words = ["schema", "column", "columns", "field", "fields", "table", "data type", "datatype", "what is in"]
    return any(word in lowered for word in schema_words)


def build_schema_answer(table_name: str) -> dict[str, Any] | None:
    settings = get_settings()
    metadata = load_schema_metadata(settings.database_url)
    table_info = metadata["tables"].get(table_name)
    if not table_info:
        return None

    lines = [f"The `{table_name}` table contains these columns:"]
    lines.append("| Column | Type | Used for |")
    lines.append("|---|---:|---|")
    preview_rows: list[dict[str, Any]] = []
    for column in table_info.get("columns") or []:
        lines.append(
            f"| `{column['column_name']}` | `{column['data_type']}` | {column['purpose_hint']} |"
        )
        preview_rows.append(
            {
                "column_name": column["column_name"],
                "data_type": column["data_type"],
                "purpose_hint": column["purpose_hint"],
            }
        )

    return {
        "question": "",
        "mode": "",
        "model": "local-fast-path",
        "answer": "\n".join(lines),
        "sources": [],
        "used_web": False,
        "used_database": True,
        "tool_events": [{"tool": "lookup_schema", "arguments": {"table_name": table_name, "search": ""}, "ok": True}],
        "sql_queries": [],
        "data_preview_label": "schema_columns",
        "data_preview_rows": preview_rows,
        "error": None,
    }


def normalize_booking_counts(booking_status_counts: dict[str, int]) -> dict[str, int | None]:
    normalized = {clean_text(key).casefold(): int(value) for key, value in booking_status_counts.items()}
    sold = 0
    unsold = 0
    booked = 0
    total = sum(normalized.values()) if normalized else None

    for key, value in normalized.items():
        normalized_key = " ".join(re.split(r"[^a-z0-9]+", key)).strip()
        if normalized_key in {"unsold", "available", "vacant", "not sold", "not booked", "unbooked", "not allotted"}:
            unsold += value
        elif normalized_key in {"booked", "allotted", "sold", "reserved"}:
            sold += value
            if normalized_key == "booked":
                booked += value

    return {
        "sold": sold if total is not None else None,
        "unsold": unsold if total is not None else None,
        "booked": booked if booked else None,
        "total": total,
    }


def summarize_professional_sections(raw_json: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    professional_detail = raw_json.get("ProjectProFessionAlDetail")
    if not isinstance(professional_detail, dict):
        return {}

    section_summaries: dict[str, list[dict[str, Any]]] = {}
    for section, rows in professional_detail.items():
        if not isinstance(rows, list):
            continue
        summary_rows: list[dict[str, Any]] = []
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            summary = {
                key: json_safe(row.get(key))
                for key in [
                    "Name",
                    "Type",
                    "ContactNumber",
                    "Email",
                    "RegistrationNo",
                    "COARegistrationNo",
                    "Address",
                ]
                if row.get(key) not in (None, "", [], {})
            }
            if summary:
                summary_rows.append(summary)
        if summary_rows:
            section_summaries[section] = summary_rows
    return section_summaries


def list_professional_section_names(raw_json: dict[str, Any]) -> list[str]:
    professional_detail = raw_json.get("ProjectProFessionAlDetail")
    if not isinstance(professional_detail, dict):
        return []
    return [
        section
        for section, rows in professional_detail.items()
        if isinstance(rows, list) and rows
    ][:15]


def extract_project_reference(question: str) -> str | None:
    cleaned = clean_text(question)
    registration_match = re.search(r"(RAJ/[A-Z0-9/.-]+)", cleaned, flags=re.IGNORECASE)
    if registration_match:
        return registration_match.group(1)

    patterns = [
        r"(?:for|of)\s+(.+?)(?:\?|$)",
        r"project\s+(.+?)(?:\?|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            reference = clean_text(match.group(1)).strip(" .,:;")
            if reference:
                return reference
    return None


def question_looks_like_project_summary(question: str) -> bool:
    lowered = clean_text(question).casefold()
    markers = [
        "booking status",
        "key project details",
        "summarize",
        "summary of",
        "project details",
        "tell me about project",
    ]
    return any(marker in lowered for marker in markers)


def build_project_summary_answer(project: dict[str, Any]) -> dict[str, Any]:
    raw_json_summary = project.get("raw_json_summary") or {}
    booking_counts = normalize_booking_counts(raw_json_summary.get("booking_status_counts") or {})
    booked_label = booking_counts["sold"] if booking_counts["sold"] is not None else "NA"
    total_label = booking_counts["total"] if booking_counts["total"] is not None else "NA"

    lines = [
        f"### {project.get('project_name') or 'Project'}",
        f"**Booking status:** `{booked_label} / {total_label}` sold-or-booked vs total inventory records.",
    ]
    if booking_counts["unsold"] is not None:
        lines.append(f"**Unsold inventory records:** `{booking_counts['unsold']}`.")
    if booking_counts["booked"] is not None:
        lines.append(f"**Explicitly marked booked:** `{booking_counts['booked']}`.")

    lines.extend(
        [
            "### Key details",
            f"- Registration: `{project.get('registration_no') or 'NA'}`",
            f"- Promoter: {project.get('promoter_name') or 'NA'}",
            f"- Type: {project.get('project_type') or 'NA'}",
            f"- Status: {project.get('project_status') or 'NA'}",
            f"- Location: {', '.join(part for part in [project.get('village_name'), project.get('tahsil_name'), project.get('district_name')] if clean_text(part)) or 'NA'}",
            f"- Approved year: `{project.get('approved_year') or 'NA'}`",
            f"- Land area: `{project.get('area_sqm') or 'NA'}` sqm",
            f"- Phase area: `{project.get('phase_area_sqm') or 'NA'}` sqm",
            f"- Saleable area: `{project.get('saleable_area_sqm') or 'NA'}` sqm",
            f"- Total buildings: `{project.get('total_building_count') or 'NA'}`",
            f"- Sanctioned / not sanctioned: `{project.get('sanctioned_building_count') or 'NA'} / {project.get('not_sanctioned_building_count') or 'NA'}`",
        ]
    )

    if raw_json_summary:
        if raw_json_summary.get("document_count") is not None:
            lines.append(f"- Documents tracked in raw JSON: `{raw_json_summary['document_count']}`")
        if raw_json_summary.get("professional_sections"):
            lines.append(
                "- Professional sections available: "
                + ", ".join(str(section) for section in raw_json_summary["professional_sections"][:8])
            )
        professional_directory = raw_json_summary.get("professional_directory") or {}
        for section, rows in list(professional_directory.items())[:6]:
            names = [clean_text(row.get("Name")) for row in rows if clean_text(row.get("Name"))]
            if names:
                lines.append(f"- {section}: " + ", ".join(names[:4]))

    preview_rows = [project]
    return {
        "question": "",
        "mode": "",
        "model": "local-fast-path",
        "answer": "\n".join(lines),
        "sources": [],
        "used_web": False,
        "used_database": True,
        "tool_events": [{"tool": "lookup_projects", "arguments": {"query_text": project.get("project_name") or "", "include_raw_json": True, "limit": 1}, "ok": True}],
        "sql_queries": [],
        "data_preview_label": "projects",
        "data_preview_rows": truncate_value(preview_rows, max_items=1, max_string=220),
        "error": None,
    }


def question_looks_like_project_count(question: str) -> bool:
    lowered = clean_text(question).casefold()
    return "how many" in lowered and "project" in lowered


def build_project_count_answer(question: str) -> dict[str, Any] | None:
    lowered = clean_text(question).casefold()
    district = "Jaipur" if "jaipur" in lowered else None
    sql = "SELECT COUNT(*) AS project_count FROM rera_projects"
    params: list[Any] = []
    if district:
        sql += " WHERE district_name ILIKE %s"
        params.append(f"%{district}%")

    settings = get_settings()
    with get_connection(settings.database_url) as connection:
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()

    project_count = row["project_count"] if row else 0
    area_label = f" in {district}" if district else ""
    answer = f"There are **{project_count:,}** projects{area_label} in the local `rera_projects` database."
    return {
        "question": "",
        "mode": "",
        "model": "local-fast-path",
        "answer": answer,
        "sources": [],
        "used_web": False,
        "used_database": True,
        "tool_events": [{"tool": "run_safe_sql", "arguments": {"sql": sql}, "ok": True}],
        "sql_queries": [{"tool": "run_safe_sql", "sql": sql}],
        "data_preview_label": "rows",
        "data_preview_rows": [{"project_count": project_count, "district": district or "All"}],
        "error": None,
    }


def try_fast_path(question: str, mode: str) -> dict[str, Any] | None:
    if mode == "Internet only":
        return None

    table_name = extract_known_table_name(question)
    if table_name and question_looks_like_schema_question(question):
        return build_schema_answer(table_name)

    if question_looks_like_project_summary(question):
        reference = extract_project_reference(question)
        if reference:
            settings = get_settings()
            with get_connection(settings.database_url) as connection:
                connection.read_only = True
                lookup = tool_lookup_projects(
                    connection,
                    registration_no=reference if reference.upper().startswith("RAJ/") else None,
                    query_text=None if reference.upper().startswith("RAJ/") else reference,
                    include_raw_json=True,
                    limit=3,
                )
            projects = lookup.get("projects") or []
            if projects:
                reference_key = clean_text(reference).casefold()
                exact = next(
                    (
                        project
                        for project in projects
                        if reference_key
                        in {
                            clean_text(project.get("project_name")).casefold(),
                            clean_text(project.get("registration_no")).casefold(),
                        }
                    ),
                    None,
                )
                return build_project_summary_answer(exact or projects[0])

    if question_looks_like_project_count(question) and "market" not in clean_text(question).casefold():
        return build_project_count_answer(question)

    return None


def preview_json_value(value: Any, limit: int = 180) -> str:
    rendered = json.dumps(json_safe(value), ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return rendered if len(rendered) <= limit else f"{rendered[:limit]}..."


def truncate_value(value: Any, *, max_items: int = 8, max_string: int = 600) -> Any:
    value = json_safe(value)
    if isinstance(value, str):
        return value if len(value) <= max_string else f"{value[:max_string]}..."
    if isinstance(value, list):
        return [truncate_value(item, max_items=max_items, max_string=max_string) for item in value[:max_items]]
    if isinstance(value, dict):
        return {key: truncate_value(item, max_items=max_items, max_string=max_string) for key, item in list(value.items())[:30]}
    return value


def count_key_values(data: Any, key_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == key_name and value not in (None, ""):
                    label = clean_text(value) or "Unknown"
                    counts[label] = counts.get(label, 0) + 1
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return counts


def prune_dict(data: Any, keys: list[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {key: json_safe(data.get(key)) for key in keys if key in data and data.get(key) not in (None, "", [], {})}


def summarize_raw_json(raw_json: Any) -> dict[str, Any]:
    if not isinstance(raw_json, dict):
        return {}

    booking_status_counts = count_key_values(raw_json, "BookingStatus")
    project_basic = prune_dict(
        raw_json.get("GetProjectBasic"),
        [
            "ProjectName",
            "ProjectType",
            "ProjectStatus",
            "ProjectCategory",
            "Area",
            "DistrictName",
            "TahsilName",
            "VillageName",
            "NoOfApartment",
            "NoOfPlot",
            "NoOfPlots",
            "NoOfShops",
            "NoOfGarages",
        ],
    )
    area_facilities = prune_dict(
        raw_json.get("GetProjectAreaFacilities"),
        [
            "TotalOpenArea",
            "TotalCoveredArea",
            "TotalAreaOfLand",
            "TotalRoadArea",
            "TotalParkArea",
            "TotalBUA",
            "BUA",
        ],
    )
    summary: dict[str, Any] = {
        "available_sections": list(raw_json.keys())[:25],
        "project_basic": project_basic,
        "project_area_facilities": area_facilities,
        "building_count": len(raw_json.get("GetBuildingDetails") or []) if isinstance(raw_json.get("GetBuildingDetails"), list) else None,
        "document_count": len(raw_json.get("GetDocumentsList") or []) if isinstance(raw_json.get("GetDocumentsList"), list) else None,
        "professional_sections": list_professional_section_names(raw_json),
        "professional_directory": summarize_professional_sections(raw_json),
        "booking_status_counts": booking_status_counts,
    }
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def tool_lookup_schema(connection, *, table_name: str | None = None, search: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    metadata = load_schema_metadata(settings.database_url)
    tables = metadata["tables"]
    search_text = clean_text(search).casefold()
    wanted_table = clean_text(table_name).casefold()
    matched_tables: list[dict[str, Any]] = []

    for current_table, table_info in tables.items():
        if wanted_table and current_table.casefold() != wanted_table:
            continue
        columns = table_info.get("columns") or []
        if search_text:
            if search_text not in current_table.casefold() and not any(
                search_text in column["column_name"].casefold() for column in columns
            ):
                continue
        matched_tables.append(
            {
                "table_name": current_table,
                "columns": columns,
            }
        )

    return {
        "ok": True,
        "matched_table_count": len(matched_tables),
        "matched_tables": matched_tables[:10],
    }


def build_project_where_clause(
    *,
    registration_no: str | None = None,
    query_text: str | None = None,
    district_name: str | None = None,
    promoter_name: str | None = None,
    project_type: str | None = None,
    project_status: str | None = None,
    approved_year_from: int | None = None,
    approved_year_to: int | None = None,
) -> tuple[list[str], list[Any]]:
    where_clauses: list[str] = []
    params: list[Any] = []

    if registration_no:
        where_clauses.append("registration_no ILIKE %s")
        params.append(f"%{clean_text(registration_no)}%")
    if district_name:
        where_clauses.append("district_name ILIKE %s")
        params.append(f"%{clean_text(district_name)}%")
    if promoter_name:
        where_clauses.append("promoter_name ILIKE %s")
        params.append(f"%{clean_text(promoter_name)}%")
    if project_type:
        where_clauses.append("project_type ILIKE %s")
        params.append(f"%{clean_text(project_type)}%")
    if project_status:
        where_clauses.append("project_status ILIKE %s")
        params.append(f"%{clean_text(project_status)}%")
    if approved_year_from is not None:
        where_clauses.append("approved_year >= %s")
        params.append(int(approved_year_from))
    if approved_year_to is not None:
        where_clauses.append("approved_year <= %s")
        params.append(int(approved_year_to))
    if query_text:
        text = f"%{clean_text(query_text)}%"
        where_clauses.append(
            """
            (
                registration_no ILIKE %s
                OR project_name ILIKE %s
                OR promoter_name ILIKE %s
                OR district_name ILIKE %s
                OR tahsil_name ILIKE %s
                OR village_name ILIKE %s
            )
            """
        )
        params.extend([text, text, text, text, text, text])

    return where_clauses, params


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: json_safe(value) for key, value in row.items()} for row in rows]


def tool_lookup_projects(
    connection,
    *,
    registration_no: str | None = None,
    query_text: str | None = None,
    district_name: str | None = None,
    promoter_name: str | None = None,
    project_type: str | None = None,
    project_status: str | None = None,
    approved_year_from: int | None = None,
    approved_year_to: int | None = None,
    include_raw_json: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 20))
    approved_year_from = int(approved_year_from) if approved_year_from not in (None, "") else None
    approved_year_to = int(approved_year_to) if approved_year_to not in (None, "") else None
    if approved_year_from is not None and approved_year_from <= 0:
        approved_year_from = None
    if approved_year_to is not None and approved_year_to <= 0:
        approved_year_to = None
    where_clauses, params = build_project_where_clause(
        registration_no=registration_no,
        query_text=query_text,
        district_name=district_name,
        promoter_name=promoter_name,
        project_type=project_type,
        project_status=project_status,
        approved_year_from=approved_year_from,
        approved_year_to=approved_year_to,
    )

    sql = """
    SELECT
        id,
        encrypted_project_id,
        registration_no,
        project_name,
        district_name,
        tahsil_name,
        village_name,
        promoter_name,
        project_type,
        project_status,
        approved_year,
        area_sqm,
        phase_area_sqm,
        saleable_area_sqm,
        total_building_count,
        sanctioned_building_count,
        not_sanctioned_building_count,
        COALESCE(last_changed_at, last_scraped_at, created_at) AS latest_activity_at,
        raw_json
    FROM rera_projects
    """
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += """
    ORDER BY
        CASE
            WHEN registration_no ILIKE %s THEN 0
            WHEN project_name ILIKE %s THEN 1
            ELSE 2
        END,
        COALESCE(last_changed_at, last_scraped_at, created_at) DESC
    LIMIT %s
    """
    exact_text = f"%{clean_text(registration_no or query_text)}%" if clean_text(registration_no or query_text) else "%"
    query_params = params + [exact_text, exact_text, limit]

    with connection.cursor() as cursor:
        cursor.execute(sql, query_params)
        rows = cursor.fetchall()

    projects: list[dict[str, Any]] = []
    for row in rows:
        project = {
            "registration_no": row["registration_no"],
            "project_name": row["project_name"],
            "district_name": row["district_name"],
            "tahsil_name": row["tahsil_name"],
            "village_name": row["village_name"],
            "promoter_name": row["promoter_name"],
            "project_type": row["project_type"],
            "project_status": row["project_status"],
            "approved_year": json_safe(row["approved_year"]),
            "area_sqm": json_safe(row["area_sqm"]),
            "phase_area_sqm": json_safe(row["phase_area_sqm"]),
            "saleable_area_sqm": json_safe(row["saleable_area_sqm"]),
            "total_building_count": json_safe(row["total_building_count"]),
            "sanctioned_building_count": json_safe(row["sanctioned_building_count"]),
            "not_sanctioned_building_count": json_safe(row["not_sanctioned_building_count"]),
            "latest_activity_at": json_safe(row["latest_activity_at"]),
        }
        if include_raw_json and row.get("raw_json"):
            project["raw_json_summary"] = summarize_raw_json(row["raw_json"])
        projects.append(project)

    return {
        "ok": True,
        "sql": sql.strip(),
        "project_count": len(projects),
        "projects": projects,
    }


def tool_lookup_changes(
    connection,
    *,
    registration_no: str | None = None,
    query_text: str | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 30))
    filters: list[str] = []
    params: list[Any] = []

    if registration_no:
        filters.append("p.registration_no ILIKE %s")
        params.append(f"%{clean_text(registration_no)}%")
    if query_text:
        text = f"%{clean_text(query_text)}%"
        filters.append(
            """
            (
                p.registration_no ILIKE %s
                OR p.project_name ILIKE %s
                OR p.promoter_name ILIKE %s
                OR c.field_path ILIKE %s
            )
            """
        )
        params.extend([text, text, text, text])

    sql = """
    SELECT
        p.registration_no,
        p.project_name,
        p.promoter_name,
        c.field_path,
        c.change_type,
        c.old_value,
        c.new_value,
        c.changed_at
    FROM rera_project_changes c
    JOIN rera_projects p ON p.id = c.project_id
    """
    if filters:
        sql += " WHERE " + " AND ".join(filters)
    sql += " ORDER BY c.changed_at DESC LIMIT %s"
    params.append(limit)

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    normalized = []
    for row in rows:
        normalized.append(
            {
                "registration_no": row["registration_no"],
                "project_name": row["project_name"],
                "promoter_name": row["promoter_name"],
                "field_path": row["field_path"],
                "change_type": row["change_type"],
                "old_value_preview": preview_json_value(row["old_value"]),
                "new_value_preview": preview_json_value(row["new_value"]),
                "changed_at": json_safe(row["changed_at"]),
            }
        )

    return {
        "ok": True,
        "sql": sql.strip(),
        "change_count": len(normalized),
        "changes": normalized,
    }


def tool_lookup_market_data(
    connection,
    *,
    registration_no: str | None = None,
    query_text: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 20))
    filters: list[str] = []
    params: list[Any] = []

    if registration_no:
        filters.append("p.registration_no ILIKE %s")
        params.append(f"%{clean_text(registration_no)}%")
    if query_text:
        text = f"%{clean_text(query_text)}%"
        filters.append(
            """
            (
                p.registration_no ILIKE %s
                OR p.project_name ILIKE %s
                OR p.promoter_name ILIKE %s
            )
            """
        )
        params.extend([text, text, text])

    where_sql = (" WHERE " + " AND ".join(filters)) if filters else ""

    market_sql = f"""
    SELECT
        p.registration_no,
        p.project_name,
        mp.source,
        mp.source_url,
        mp.listing_title,
        mp.price,
        mp.area,
        mp.price_per_sqft,
        mp.notes,
        mp.confidence_score,
        mp.recorded_at
    FROM project_market_prices mp
    JOIN rera_projects p ON p.id = mp.project_id
    {where_sql}
    ORDER BY mp.recorded_at DESC
    LIMIT %s
    """

    roi_sql = f"""
    SELECT
        p.registration_no,
        p.project_name,
        roi.scenario_name,
        roi.purchase_price,
        roi.expected_sale_price,
        roi.holding_period_months,
        roi.total_investment,
        roi.net_profit,
        roi.roi_pct,
        roi.annualized_roi_pct,
        roi.created_at
    FROM project_roi_cases roi
    JOIN rera_projects p ON p.id = roi.project_id
    {where_sql}
    ORDER BY roi.created_at DESC
    LIMIT %s
    """

    candidate_sql = f"""
    SELECT
        p.registration_no,
        p.project_name,
        c.source,
        c.source_url,
        c.result_title,
        c.extracted_price_text,
        c.extracted_price_value,
        c.price_per_sqft,
        c.confidence_score,
        c.created_at
    FROM project_price_candidates c
    JOIN rera_projects p ON p.id = c.project_id
    {where_sql}
    ORDER BY c.confidence_score DESC NULLS LAST, c.created_at DESC
    LIMIT %s
    """

    query_params = params + [limit]
    with connection.cursor() as cursor:
        cursor.execute(market_sql, query_params)
        market_rows = cursor.fetchall()
        cursor.execute(roi_sql, query_params)
        roi_rows = cursor.fetchall()
        cursor.execute(candidate_sql, query_params)
        candidate_rows = cursor.fetchall()

    return {
        "ok": True,
        "market_prices_sql": market_sql.strip(),
        "roi_cases_sql": roi_sql.strip(),
        "price_candidates_sql": candidate_sql.strip(),
        "market_prices": normalize_rows(market_rows),
        "roi_cases": normalize_rows(roi_rows),
        "price_candidates": normalize_rows(candidate_rows),
    }


def tool_calculate_roi(
    *,
    purchase_price: float,
    expected_sale_price: float,
    holding_period_months: int,
    stamp_duty: float = 0,
    registration: float = 0,
    brokerage: float = 0,
    other_cost: float = 0,
) -> dict[str, Any]:
    return {
        "ok": True,
        "roi_case": calculate_roi_metrics(
            purchase_price=float(purchase_price),
            stamp_duty=float(stamp_duty),
            registration=float(registration),
            brokerage=float(brokerage),
            other_cost=float(other_cost),
            expected_sale_price=float(expected_sale_price),
            holding_period_months=int(holding_period_months),
        ),
    }


def sql_without_comments(sql: str) -> str:
    sql = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql.strip()


def extract_cte_names(sql: str) -> set[str]:
    lowered = sql.casefold()
    return {match.group(1) for match in re.finditer(r"(?:with|,)\s*([a-z_][a-z0-9_]*)\s+as\s*\(", lowered)}


def validate_safe_sql(sql: str) -> tuple[bool, str]:
    cleaned = sql_without_comments(sql)
    if not cleaned:
        return False, "SQL is empty."
    one_statement = cleaned.rstrip().rstrip(";").strip()
    if ";" in one_statement:
        return False, "Only one SQL statement is allowed."
    if "$$" in one_statement:
        return False, "Dollar-quoted SQL is not allowed."
    lowered = one_statement.casefold()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False, "Only SELECT or WITH queries are allowed."
    for pattern in AI_CHAT_DISALLOWED_SQL_PATTERNS:
        if re.search(pattern, lowered):
            return False, f"Blocked unsafe SQL pattern: {pattern}"

    cte_names = extract_cte_names(one_statement)
    table_refs = re.findall(r"\b(?:from|join)\s+([a-z_][a-z0-9_.]*)", lowered)
    for ref in table_refs:
        if ref in cte_names:
            continue
        if "." in ref:
            schema_name, table_name = ref.split(".", 1)
            if schema_name != "public":
                return False, f"Only public schema tables are allowed, found {ref}."
        else:
            table_name = ref
        if table_name not in AI_CHAT_ALLOWED_TABLES:
            return False, f"Table {table_name} is not allowed."

    for match in re.finditer(r"(?<![\w.])([a-z_][a-z0-9_.]*)\(", lowered):
        function_name = match.group(1)
        if "." in function_name:
            return False, f"Schema-qualified function {function_name} is not allowed."
        if function_name not in AI_CHAT_ALLOWED_SQL_FUNCTIONS:
            return False, f"Function {function_name} is not allowed in the generic SQL tool."

    return True, one_statement


def add_limit_if_missing(sql: str, limit: int = AI_CHAT_SQL_MAX_ROWS) -> str:
    cleaned = sql.rstrip().rstrip(";").strip()
    if re.search(r"\blimit\s+\d+\b", cleaned, flags=re.IGNORECASE):
        return cleaned
    return f"{cleaned}\nLIMIT {limit}"


def tool_run_safe_sql(connection, *, sql: str) -> dict[str, Any]:
    is_safe, safe_or_reason = validate_safe_sql(sql)
    if not is_safe:
        return {"ok": False, "error": safe_or_reason}

    final_sql = add_limit_if_missing(safe_or_reason)
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL statement_timeout = '{AI_CHAT_SQL_STATEMENT_TIMEOUT_MS}ms'")
        cursor.execute(final_sql)
        rows = cursor.fetchall()
        columns = [desc.name for desc in cursor.description] if cursor.description else []

    normalized_rows = normalize_rows(rows)
    return {
        "ok": True,
        "sql": final_sql,
        "columns": columns,
        "row_count": len(normalized_rows),
        "rows": normalized_rows[:50],
    }


@dataclass
class ToolRuntime:
    sql_queries: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    evidence_blocks: list[dict[str, Any]] = field(default_factory=list)
    data_preview_label: str | None = None
    data_preview_rows: list[dict[str, Any]] = field(default_factory=list)
    used_database: bool = False

    def record(self, name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        event = {
            "tool": name,
            "arguments": json_safe(arguments),
            "ok": bool(result.get("ok", True)),
        }
        if "error" in result:
            event["error"] = clean_text(result["error"])
        self.tool_events.append(event)
        self.evidence_blocks.append(
            {
                "tool": name,
                "result": truncate_value(result),
            }
        )
        if name == "lookup_schema":
            self.used_database = True

        for key in ("sql", "market_prices_sql", "roi_cases_sql", "price_candidates_sql"):
            if key in result and result[key]:
                self.used_database = True
                self.sql_queries.append({"tool": name, "sql": result[key]})

        for key in ("rows", "projects", "changes", "market_prices", "roi_cases", "price_candidates"):
            rows = result.get(key)
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                self.used_database = True
                self.data_preview_label = key
                self.data_preview_rows = truncate_value(rows[:50], max_items=50, max_string=300)
                break


def get_tool_definitions(mode: str) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    db_allowed = mode in {"Auto", "Database only", "Database + Internet"}
    web_allowed = mode in {"Auto", "Internet only", "Database + Internet"}

    if db_allowed:
        tools.extend(
            [
                {
                    "type": "function",
                    "name": "lookup_schema",
                    "description": "Inspect allowed database schema tables and columns. Use this for any question about tables, columns, data types, field availability, or what a database structure contains.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "table_name": {"type": "string", "description": "Optional exact table name to inspect."},
                            "search": {"type": "string", "description": "Optional free-text search for matching table or column names."},
                        },
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "lookup_projects",
                    "description": "Find projects in the local RERA database and return project-level details. Use this for specific projects, promoters, filters, and raw project detail questions.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "registration_no": {"type": "string"},
                            "query_text": {"type": "string"},
                            "district_name": {"type": "string"},
                            "promoter_name": {"type": "string"},
                            "project_type": {"type": "string"},
                            "project_status": {"type": "string"},
                            "approved_year_from": {"type": "integer"},
                            "approved_year_to": {"type": "integer"},
                            "include_raw_json": {"type": "boolean"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "lookup_changes",
                    "description": "Look up tracked project changes and update history for projects.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "registration_no": {"type": "string"},
                            "query_text": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                        },
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "lookup_market_data",
                    "description": "Get market prices, price candidates, and saved ROI cases related to projects.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "registration_no": {"type": "string"},
                            "query_text": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "calculate_roi",
                    "description": "Calculate a fresh ROI scenario from user-supplied numbers when the question is mathematical rather than asking for existing saved scenarios.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "purchase_price": {"type": "number"},
                            "expected_sale_price": {"type": "number"},
                            "holding_period_months": {"type": "integer"},
                            "stamp_duty": {"type": "number"},
                            "registration": {"type": "number"},
                            "brokerage": {"type": "number"},
                            "other_cost": {"type": "number"},
                        },
                        "required": ["purchase_price", "expected_sale_price", "holding_period_months"],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "run_safe_sql",
                    "description": "Run one read-only SQL query against the allowed business tables for aggregates, comparisons, trends, or custom analytics. Use lookup_schema first for schema questions. Use simple unquoted SQL only.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sql": {"type": "string", "description": "Exactly one plain unquoted SELECT or WITH query against allowed public tables."},
                        },
                        "required": ["sql"],
                        "additionalProperties": False,
                    },
                },
            ]
        )

    if web_allowed:
        tools.append({"type": "web_search", "search_context_size": "medium"})

    return tools


def build_history_context(history: list[dict[str, Any]] | None) -> str:
    if not history:
        return "No previous AI chat turns."

    snippets: list[str] = []
    for item in list(history)[:AI_CHAT_MAX_HISTORY_TURNS]:
        answer = clean_text(item.get("answer"))[:1200]
        preview = item.get("data_preview_rows") or []
        preview_text = json.dumps(preview[:5], ensure_ascii=False)[:1200] if preview else ""
        snippets.append(
            "Previous turn:\n"
            f"Question: {clean_text(item.get('question'))}\n"
            f"Answer: {answer}\n"
            f"Data preview: {preview_text}"
        )
    return "\n\n".join(snippets)


def get_system_instructions(mode: str) -> str:
    mode_instructions = {
        "Database only": "Use only the database tools. Do not use web search.",
        "Internet only": "Use only web search and general reasoning. Do not call database tools. Use web search before answering.",
        "Database + Internet": "Use both database tools and web search when helpful.",
        "Auto": "Choose intelligently between database tools, web search, or both.",
    }
    return (
        "You are the AI analyst for a Rajasthan RERA intelligence dashboard.\n"
        "Your job is to answer questions about database schema, project records, market evidence, ROI, change history, and external real-estate context.\n"
        "Tool rules:\n"
        "- For schema/table/column questions, call lookup_schema first.\n"
        "- For specific projects or promoter/location filtered project questions, call lookup_projects.\n"
        "- For change history, call lookup_changes.\n"
        "- For market prices, candidates, or saved ROI cases, call lookup_market_data.\n"
        "- For ad hoc ROI math, call calculate_roi.\n"
        "- For broader analytics, comparisons, counts, rankings, or trends, call run_safe_sql.\n"
        "- For latest external news, infrastructure, regulations, demand trends, or reputation checks, use web search.\n"
        "- For mixed questions, use both local database evidence and web search when useful.\n"
        "- For project-detail questions, prefer one lookup_projects call, and if booking or project detail is needed include raw_json in that same call.\n"
        "- Once you have enough evidence to answer, stop calling tools and answer directly.\n"
        "- Avoid repeated exploratory retries with only minor argument changes.\n"
        "- Prefer one well-designed SQL query over several small exploratory SQL queries.\n"
        "- Aim to finish within three tool calls unless a previous tool call returned no useful result.\n"
        "- Prefer the database for factual statements about the user's local data.\n"
        "- Never claim to have used a tool if you did not.\n"
        "- If a tool returns an error, recover by using another appropriate tool or explain the limitation.\n"
        "- Keep answers concise, decision-useful, and grounded.\n"
        "- When many projects match, summarize the top five unless the user explicitly asks for a larger list.\n"
        "- For micro-market questions, mention that village/tehsil names are an approximate proxy.\n"
        f"Mode rule: {mode_instructions.get(mode, mode_instructions['Auto'])}"
    )


def execute_tool_call(connection, runtime: ToolRuntime, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "lookup_schema":
            result = tool_lookup_schema(connection, **arguments)
        elif name == "lookup_projects":
            result = tool_lookup_projects(connection, **arguments)
        elif name == "lookup_changes":
            result = tool_lookup_changes(connection, **arguments)
        elif name == "lookup_market_data":
            result = tool_lookup_market_data(connection, **arguments)
        elif name == "calculate_roi":
            result = tool_calculate_roi(**arguments)
        elif name == "run_safe_sql":
            result = tool_run_safe_sql(connection, **arguments)
        else:
            result = {"ok": False, "error": f"Unknown tool: {name}"}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}

    runtime.record(name, arguments, result)
    return result


def ensure_ai_chat_log_table() -> None:
    settings = get_settings()
    with get_connection(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(AI_CHAT_LOG_TABLE_SQL)


def log_ai_chat_result(payload: dict[str, Any]) -> None:
    AI_CHAT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AI_CHAT_LOG_PATH.open("a", encoding="utf-8") as file_handle:
        file_handle.write(json.dumps(json_safe(payload), ensure_ascii=False) + "\n")

    settings = get_settings()
    try:
        ensure_ai_chat_log_table()
        with get_connection(settings.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ai_chat_logs (
                        mode, model, question, answer, tool_trace, sql_queries, sources, error_text
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)
                    """,
                    (
                        payload.get("mode"),
                        payload.get("model"),
                        payload.get("question"),
                        payload.get("answer"),
                        json.dumps(json_safe(payload.get("tool_events") or []), ensure_ascii=False),
                        json.dumps(json_safe(payload.get("sql_queries") or []), ensure_ascii=False),
                        json.dumps(json_safe(payload.get("sources") or []), ensure_ascii=False),
                        payload.get("error"),
                    ),
                )
    except Exception:
        # File logging is the durability fallback if DB logging fails.
        return


def ask_ai_chat(question: str, mode: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in local env or Streamlit secrets.")

    fast_result = try_fast_path(question, mode)
    if fast_result is not None:
        fast_result["question"] = question.strip()
        fast_result["mode"] = mode
        log_ai_chat_result(fast_result)
        return fast_result

    client = get_openai_client()
    model = get_ai_chat_model()
    history_context = build_history_context(history)
    user_message = (
        f"Previous conversation context:\n{history_context}\n\n"
        f"Current user question:\n{question.strip()}\n"
    )

    input_items: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    tools = get_tool_definitions(mode)
    runtime = ToolRuntime()
    final_text = ""
    final_sources: list[dict[str, str]] = []
    used_web = False

    with get_connection(settings.database_url) as connection:
        connection.read_only = True
        response = client.responses.create(
            model=model,
            instructions=get_system_instructions(mode),
            input=input_items,
            tools=tools,
            parallel_tool_calls=False,
            reasoning={"effort": "low"},
            max_output_tokens=2200,
        )
        for _step in range(AI_CHAT_MAX_TOOL_STEPS):
            text, sources, response_used_web = extract_text_and_sources(response)
            used_web = used_web or response_used_web
            function_calls = [item for item in response.output if getattr(item, "type", None) == "function_call"]

            if not function_calls:
                final_text = text
                final_sources = sources
                break

            tool_outputs: list[dict[str, Any]] = []
            for function_call in function_calls:
                try:
                    arguments = json.loads(function_call.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                result = execute_tool_call(connection, runtime, function_call.name, arguments)
                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": function_call.call_id,
                        "output": json.dumps(json_safe(result), ensure_ascii=False),
                    }
                )
            response = client.responses.create(
                model=model,
                previous_response_id=response.id,
                input=tool_outputs,
                tools=tools,
                parallel_tool_calls=False,
                reasoning={"effort": "low"},
                max_output_tokens=2200,
            )
        else:
            response = client.responses.create(
                model=model,
                instructions=(
                    "You are finishing an AI dashboard answer after tool usage was stopped. "
                    "Do not call tools. Use only the supplied evidence. "
                    "If some detail is incomplete, say that clearly."
                ),
                input=json.dumps(
                    {
                        "question": question.strip(),
                        "tool_events": runtime.tool_events,
                        "sql_queries": runtime.sql_queries,
                        "evidence_blocks": runtime.evidence_blocks,
                        "data_preview_label": runtime.data_preview_label,
                        "data_preview_rows": runtime.data_preview_rows[:20],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                reasoning={"effort": "low"},
                max_output_tokens=2200,
            )
            final_text, final_sources, response_used_web = extract_text_and_sources(response)
            used_web = used_web or response_used_web
            if not final_text:
                raise RuntimeError("The AI agent could not produce a final answer after the tool limit.")
            final_sources = final_sources or []

    result = {
        "question": question.strip(),
        "mode": mode,
        "model": model,
        "answer": final_text,
        "sources": final_sources,
        "used_web": used_web or bool(final_sources),
        "used_database": runtime.used_database,
        "tool_events": runtime.tool_events,
        "sql_queries": runtime.sql_queries,
        "data_preview_label": runtime.data_preview_label,
        "data_preview_rows": runtime.data_preview_rows,
        "error": None,
    }
    log_ai_chat_result(result)
    return result


def safe_ask_ai_chat(question: str, mode: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    try:
        return ask_ai_chat(question, mode, history)
    except Exception as exc:  # noqa: BLE001
        result = {
            "question": question.strip(),
            "mode": mode,
            "model": get_ai_chat_model(),
            "answer": "",
            "sources": [],
            "used_web": False,
            "used_database": False,
            "tool_events": [],
            "sql_queries": [],
            "data_preview_label": None,
            "data_preview_rows": [],
            "error": str(exc),
        }
        log_ai_chat_result(result)
        return result
