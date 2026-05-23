from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
import pydeck as pdk
import streamlit as st
from psycopg.errors import UndefinedTable

from rera_intel.config import get_settings
from rera_intel.ai_chat import (
    get_ai_chat_model,
    get_schema_summary as get_ai_chat_schema_summary,
    safe_ask_ai_chat,
)
from rera_intel.db import get_connection
from rera_intel.documents import collect_project_documents, probe_document
from rera_intel.market import (
    calculate_price_per_sqft,
    insert_project_market_price,
    insert_project_roi_case,
    load_project_market_prices,
    load_project_roi_cases,
)
from rera_intel.maps import collect_map_documents, flatten_geometry_points, parse_map_document
from rera_intel.openai_summary import summarize_remote_document
from rera_intel.price_discovery import (
    load_project_price_candidates,
    refresh_project_price_candidates,
    resolve_selected_market_price,
    run_weekly_price_sync,
)
from rera_intel.roi_calculator import calculate_roi_metrics


st.set_page_config(
    page_title="RERA Rajasthan Intelligence MVP",
    page_icon="🏗️",
    layout="wide",
)


DETAILED_MAP_STYLE = {
    "version": 8,
    "sources": {
        "carto-voyager": {
            "type": "raster",
            "tiles": [
                "https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}@2x.png"
            ],
            "tileSize": 256,
            "attribution": "OpenStreetMap contributors, CARTO",
        },
    },
    "layers": [
        {
            "id": "carto-voyager",
            "type": "raster",
            "source": "carto-voyager",
            "minzoom": 0,
            "maxzoom": 20,
        },
    ],
}

HYBRID_MAP_STYLE = {
    "version": 8,
    "sources": {
        "esri-world-imagery": {
            "type": "raster",
            "tiles": [
                "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
            ],
            "tileSize": 256,
            "attribution": "Esri, Maxar, Earthstar Geographics, and the GIS User Community",
        },
        "esri-world-transportation": {
            "type": "raster",
            "tiles": [
                "https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}"
            ],
            "tileSize": 256,
            "attribution": "Esri",
        },
        "esri-reference-labels": {
            "type": "raster",
            "tiles": [
                "https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
            ],
            "tileSize": 256,
            "attribution": "Esri",
        },
    },
    "layers": [
        {
            "id": "esri-world-imagery",
            "type": "raster",
            "source": "esri-world-imagery",
            "minzoom": 0,
            "maxzoom": 22,
        },
        {
            "id": "esri-world-transportation",
            "type": "raster",
            "source": "esri-world-transportation",
            "minzoom": 0,
            "maxzoom": 22,
        },
        {
            "id": "esri-reference-labels",
            "type": "raster",
            "source": "esri-reference-labels",
            "minzoom": 0,
            "maxzoom": 22,
        },
    ],
}

SATELLITE_MAP_STYLE = {
    "version": 8,
    "sources": HYBRID_MAP_STYLE["sources"],
    "layers": [
        HYBRID_MAP_STYLE["layers"][0],
        HYBRID_MAP_STYLE["layers"][2],
    ],
}

DETAILED_MAP_STYLE_URL = (
    "data:application/json;charset=utf-8,"
    f"{quote(json.dumps(DETAILED_MAP_STYLE, separators=(',', ':')))}"
)
HYBRID_MAP_STYLE_URL = (
    "data:application/json;charset=utf-8,"
    f"{quote(json.dumps(HYBRID_MAP_STYLE, separators=(',', ':')))}"
)
SATELLITE_MAP_STYLE_URL = (
    "data:application/json;charset=utf-8,"
    f"{quote(json.dumps(SATELLITE_MAP_STYLE, separators=(',', ':')))}"
)

MAP_STYLE_OPTIONS = {
    "Detailed": DETAILED_MAP_STYLE_URL,
    "Hybrid": HYBRID_MAP_STYLE_URL,
    "Satellite": SATELLITE_MAP_STYLE_URL,
}


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        :root {
            --sand: #0f1720;
            --ink: #e8edf2;
            --clay: #c58a62;
            --sage: #7ea497;
            --line: #2e3944;
        }
        .stApp {
            background:
                radial-gradient(circle at top left, rgba(197, 138, 98, 0.16), transparent 28%),
                radial-gradient(circle at top right, rgba(126, 164, 151, 0.14), transparent 24%),
                linear-gradient(180deg, #111821 0%, var(--sand) 100%);
            color: var(--ink);
        }
        .block-container {
            padding-top: 2rem;
        }
        .hero {
            padding: 1.2rem 1.4rem;
            border: 1px solid var(--line);
            border-radius: 18px;
            background: rgba(17, 24, 33, 0.78);
            box-shadow: 0 18px 50px rgba(0, 0, 0, 0.22);
            backdrop-filter: blur(8px);
            margin-bottom: 1rem;
        }
        .hero h1 {
            margin: 0;
            color: var(--ink);
            font-size: 2rem;
        }
        .hero p {
            margin: 0.5rem 0 0 0;
            color: #b8c4cf;
        }
        div[data-testid="stMetricValue"] {
            overflow: visible;
            text-overflow: clip;
            white-space: normal;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_missing_configuration(message: str) -> None:
    st.error(message)
    st.markdown("### Streamlit Cloud setup")
    st.markdown(
        "Add your database and API secrets in the app's **Manage app -> Settings -> Secrets** panel, then redeploy or reboot the app."
    )
    st.code(
        """DATABASE_URL = "postgresql://user:password@host:5432/rera_rajasthan"
OPENAI_API_KEY = "sk-..."
RERA_API_KEY = "your_rera_api_key"
SERPAPI_KEY = "your_serpapi_key"
SERPAPI_GL = "in"
SERPAPI_HL = "en"
SERPAPI_LOCATION = "Rajasthan, India"
OPENAI_SUMMARY_MODEL = "gpt-5.5"
""",
        language="toml",
    )
    st.caption(
        "If you already added secrets, make sure the database key is named `DATABASE_URL` "
        "or uses one of the supported nested paths such as `database.url` or `connections.postgresql.url`."
    )


def get_database_bootstrap_status() -> tuple[bool, str | None]:
    try:
        connection = get_dashboard_connection()
        with connection.cursor() as cursor:
            row = fetch_one(
                cursor,
                """
                SELECT
                    to_regclass('public.rera_projects') AS rera_projects,
                    to_regclass('public.rera_project_changes') AS rera_project_changes,
                    to_regclass('public.project_market_prices') AS project_market_prices,
                    to_regclass('public.project_roi_cases') AS project_roi_cases
                """,
            )
            if not row:
                return False, "Database check returned no result."
            missing = [
                table_name
                for table_name, regclass in row.items()
                if regclass is None
            ]
            if missing:
                return False, (
                    "Connected to PostgreSQL, but the app tables are missing: "
                    + ", ".join(missing)
                    + "."
                )

            count_row = fetch_one(cursor, "SELECT COUNT(*)::int AS project_count FROM rera_projects")
            project_count = int((count_row or {}).get("project_count") or 0)
            if project_count == 0:
                return False, (
                    "Connected to PostgreSQL, and the schema exists, but `rera_projects` is empty."
                )
    except UndefinedTable:
        return False, "Connected to PostgreSQL, but the required app tables do not exist yet."
    except Exception as exc:  # noqa: BLE001
        return False, f"Database connection or schema check failed: {exc}"

    return True, None


def render_database_setup_required(message: str) -> None:
    st.error(message)
    st.markdown("### Database setup required")
    st.markdown(
        "Your Streamlit app can reach PostgreSQL, but the RERA schema and/or data have not been loaded into that database yet."
    )
    st.markdown("Use the same Neon `DATABASE_URL` locally, then run:")
    st.code(
        """python setup_db.py
python ingest_existing_jsons.py""",
        language="bash",
    )
    st.markdown("If you also want fresh API sync support afterward, run:")
    st.code("python daily_sync.py", language="bash")
    st.caption(
        "In short: Streamlit Cloud only hosts the app. It does not automatically create your tables or import your local project data into Neon."
    )


@st.cache_resource(show_spinner=False)
def _create_dashboard_connection():
    settings = get_settings()
    connection = get_connection(settings.database_url)
    connection.autocommit = True
    return connection


def get_dashboard_connection():
    connection = _create_dashboard_connection()
    needs_reconnect = getattr(connection, "closed", False) or getattr(connection, "broken", False)
    if not needs_reconnect:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            needs_reconnect = True
    if needs_reconnect:
        _create_dashboard_connection.clear()
        connection = _create_dashboard_connection()
    return connection


def fetch_all(cursor, query: str, params: tuple[Any, ...] | list[Any] | None = None):
    cursor.execute(query, params or ())
    return cursor.fetchall()


def fetch_one(cursor, query: str, params: tuple[Any, ...] | list[Any] | None = None):
    cursor.execute(query, params or ())
    return cursor.fetchone()


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    return text or None


def registration_sort_key(value: Any) -> tuple[Any, ...]:
    text = clean_text(value) or ""
    lowered = text.casefold()
    is_exempted = "reraexempted" in lowered
    match = re.search(r"/(\d{4})/(\d+)(?:-[A-Za-z0-9]+)?$", text)
    has_numeric_parts = bool(match)
    year = int(match.group(1)) if match else 0
    sequence = int(match.group(2)) if match else 0
    return (
        1 if is_exempted else 0,
        0 if has_numeric_parts else 1,
        -year,
        -sequence,
        lowered,
    )


def text_key(value: Any) -> str | None:
    text = clean_text(value)
    return text.casefold() if text else None


def preview_value(value: Any, limit: int = 120) -> str:
    if isinstance(value, dict):
        return f"{len(value)} keys"
    if isinstance(value, list):
        return f"{len(value)} items"
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


def normalize_table_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return preview_value(value, limit=180)
    return value


def humanize_identifier(value: str) -> str:
    mapping = {
        "CA": "CA",
        "HVAC": "HVAC",
        "MEPConsultants": "MEP Consultants",
        "NewEngineer": "New Engineer",
        "ProjectAgent": "Project Agent",
        "ProjectProFessionAlDetail": "Project Professional Detail",
        "GetProjectBasic": "Project Basic",
        "GetProjectCostDetail": "Project Cost Detail",
        "GetProjectAreaFacilities": "Project Area Facilities",
        "GetBuildingDetails": "Building Details",
        "ProjectCommanArea": "Project Common Area",
        "GetApartmentAllotteeDetailsList": "Apartment Allottee Details",
        "Tbl_Plots": "Table Plots",
    }
    if value in mapping:
        return mapping[value]
    parts = []
    token = ""
    for char in value:
        if char == "_":
            if token:
                parts.append(token)
                token = ""
            continue
        if token and char.isupper() and not token[-1].isupper():
            parts.append(token)
            token = char
        else:
            token += char
    if token:
        parts.append(token)
    return " ".join(part.capitalize() for part in parts if part)


def format_currency(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"Rs {value:,.2f}"


def format_percent(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{value:.2f}%"


def number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, str):
            cleaned = value.replace(",", "").strip()
            if not cleaned:
                return None
            return float(cleaned)
        return float(value)
    except (TypeError, ValueError):
        return None


def int_display(value: Any) -> str:
    num = number_or_none(value)
    if num is None:
        return "NA"
    return str(int(num)) if float(num).is_integer() else f"{num:,.2f}"


def area_display(value: Any) -> str:
    num = number_or_none(value)
    if num is None:
        return "NA"
    return f"{num:,.2f} sqm"


def first_number_from_dict(data: dict[str, Any], keys: list[str]) -> int | None:
    for key in keys:
        value = data.get(key)
        num = number_or_none(value)
        if num is not None:
            return int(num)
    return None


def get_list_section(raw_json: dict[str, Any], section_names: list[str]) -> list[dict[str, Any]]:
    for section_name in section_names:
        value = raw_json.get(section_name)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def count_sold_rows(rows: list[dict[str, Any]]) -> int | None:
    if not rows:
        return None

    sold_words = {"sold", "booked", "allotted", "allotteed", "reserved"}
    unsold_words = {"unsold", "available", "vacant", "not sold", "not booked"}
    status_keys = [
        "Status",
        "BookingStatus",
        "AllotmentStatus",
        "SaleStatus",
        "SoldStatus",
        "UnitStatus",
        "FlatStatus",
        "PlotStatus",
        "IsSold",
        "IsBooked",
        "IsAllotted",
    ]

    matched = 0
    saw_status = False

    for row in rows:
        for key in status_keys:
            if key not in row:
                continue

            saw_status = True
            value = row.get(key)

            if isinstance(value, bool):
                if value:
                    matched += 1
                break

            text = str(value or "").strip().casefold()
            if not text:
                continue

            if any(word in text for word in unsold_words):
                break

            if text in {"1", "true", "yes", "y"} or any(word in text for word in sold_words):
                matched += 1
                break

    return matched if saw_status else None


def sum_number_from_rows(rows: list[dict[str, Any]], keys: list[str]) -> int | None:
    total = 0
    found = False

    for row in rows:
        for key in keys:
            num = number_or_none(row.get(key))
            if num is not None:
                total += int(num)
                found = True
                break

    return total if found else None


def normalize_key(value: Any) -> str:
    return re_sub_non_alnum(str(value or "").casefold())


def re_sub_non_alnum(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "", value)


def is_numeric_id_like(value: Any) -> bool:
    text = clean_text(value)
    return bool(text and text.isdigit())


def display_text(value: Any) -> str:
    text = clean_text(value)
    return text if text else "NA"


def display_tehsil(value: Any) -> str:
    text = clean_text(value)
    if not text or text.isdigit():
        return "NA"
    return text


def iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from iter_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_dicts(item)


def get_value_case_insensitive(data: dict[str, Any], keys: list[str]) -> Any:
    """Return a value from a dict using exact or normalized key matching."""
    if not isinstance(data, dict):
        return None

    normalized_lookup = {normalize_key(key): value for key, value in data.items()}
    for key in keys:
        if key in data:
            return data.get(key)
        normalized_key = normalize_key(key)
        if normalized_key in normalized_lookup:
            return normalized_lookup[normalized_key]
    return None


def first_number_case_insensitive(data: dict[str, Any], keys: list[str]) -> int | None:
    value = get_value_case_insensitive(data, keys)
    num = number_or_none(value)
    return int(num) if num is not None else None


def sum_numbers_case_insensitive(rows: list[dict[str, Any]], keys: list[str]) -> int | None:
    total = 0
    found = False
    for row in rows:
        value = first_number_case_insensitive(row, keys)
        if value is not None:
            total += value
            found = True
    return total if found else None


def list_rows_from_section(raw_json: dict[str, Any], section_names: list[str]) -> list[dict[str, Any]]:
    for section_name in section_names:
        value = raw_json.get(section_name)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            rows: list[dict[str, Any]] = []
            for nested_value in value.values():
                if isinstance(nested_value, list):
                    rows.extend(item for item in nested_value if isinstance(item, dict))
            if rows:
                return rows
    return []


def list_building_apartment_rows(raw_json: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    buildings = raw_json.get("GetBuildingDetails")
    if not isinstance(buildings, list):
        return rows
    for building in buildings:
        if not isinstance(building, dict):
            continue
        apartment_rows = building.get("GetAppartmentDetails")
        if isinstance(apartment_rows, list):
            rows.extend(item for item in apartment_rows if isinstance(item, dict))
    return rows


def row_looks_like_inventory_item(row: dict[str, Any], *, plot_mode: bool) -> bool:
    keys = {normalize_key(key) for key in row.keys()}
    if plot_mode:
        return bool(
            keys
            & {
                "plotno",
                "plotnumber",
                "plotarea",
                "plotsize",
                "plotuse",
                "plotsaleablearea",
                "areaofplot",
            }
        )
    return bool(
        keys
        & {
            "unitno",
            "flatno",
            "shopno",
            "apartmentno",
            "unitnumber",
            "flatnumber",
            "carpetarea",
            "saleablearea",
            "builtuparea",
        }
    )


def count_inventory_rows(rows: list[dict[str, Any]], *, plot_mode: bool) -> int | None:
    if not rows:
        return None
    matching = sum(1 for row in rows if row_looks_like_inventory_item(row, plot_mode=plot_mode))
    # Avoid counting summary/table rows as inventory.
    if matching >= 2 and matching >= max(1, int(len(rows) * 0.6)):
        return matching
    return None


def count_sold_from_status_rows(rows: list[dict[str, Any]]) -> int | None:
    """Only count sold/allotted if a clear status field exists.

    Do not treat the mere presence of rows in allottee sections as sold inventory,
    because some RERA payloads include all units/plots there and that produced wrong sold=total values.
    """
    if not rows:
        return None

    status_keys = [
        "Status",
        "BookingStatus",
        "AllotmentStatus",
        "SaleStatus",
        "SoldStatus",
        "UnitStatus",
        "FlatStatus",
        "PlotStatus",
        "IsSold",
        "IsBooked",
        "IsAllotted",
    ]
    sold_words = {"sold", "booked", "allotted", "allotteed", "reserved"}
    unsold_words = {
        "unsold",
        "available",
        "vacant",
        "not sold",
        "not booked",
        "not allotted",
        "not reserved",
        "unbooked",
        "unallotted",
        "free",
    }

    saw_status = False
    sold_count = 0
    for row in rows:
        for key in status_keys:
            value = get_value_case_insensitive(row, [key])
            if value is None:
                continue
            saw_status = True
            if isinstance(value, bool):
                sold_count += int(value)
                break
            text = str(value).strip().casefold()
            if not text:
                break
            normalized = " ".join(re.split(r"[^a-z0-9]+", text)).strip()
            if normalized in unsold_words:
                break
            if normalized in {"1", "true", "yes", "y"} or normalized in sold_words:
                sold_count += 1
                break
            break
    return sold_count if saw_status else None


TOTAL_INVENTORY_KEYS = [
    "TotalUnits",
    "TotalUnit",
    "TotalNoOfUnits",
    "TotalNoOfUnit",
    "NoOfUnits",
    "NoOfUnit",
    "TotalApartment",
    "TotalApartments",
    "NoOfApartment",
    "NoOfApartments",
    "TotalFlat",
    "TotalFlats",
    "NoOfFlat",
    "NoOfFlats",
    "TotalPlots",
    "TotalPlot",
    "NoOfPlots",
    "NoOfPlot",
    "NumberOfPlots",
    "NumberOfPlot",
    "TotalShop",
    "TotalShops",
    "NoOfShop",
    "NoOfShops",
    "TotalVilla",
    "TotalVillas",
    "TotalUnitCount",
    "UnitCount",
]

SOLD_INVENTORY_KEYS = [
    "SoldUnits",
    "SoldUnit",
    "TotalSoldUnits",
    "TotalSoldUnit",
    "SoldApartment",
    "SoldApartments",
    "SoldFlat",
    "SoldFlats",
    "SoldPlots",
    "SoldPlot",
    "BookedUnits",
    "BookedUnit",
    "BookedFlat",
    "BookedFlats",
    "BookedPlots",
    "BookedPlot",
    "AllottedUnits",
    "AllottedUnit",
    "AllottedFlat",
    "AllottedFlats",
    "AllottedPlots",
    "AllottedPlot",
    "ReservedUnits",
    "ReservedUnit",
    "NumberOfApartmentsBooked",
    "BookedApartment",
]


def get_inventory_summary(project: dict[str, Any]) -> dict[str, Any]:
    """Conservative inventory summary.

    It avoids the earlier false sold=total problem by never using len(allottee_rows)
    as sold count unless a row has an explicit sold/booked/allotted status.
    """
    raw_json = project.get("raw_json") or {}
    if not isinstance(raw_json, dict):
        raw_json = {}

    basic = raw_json.get("GetProjectBasic")
    if not isinstance(basic, dict):
        basic = {}

    total_candidates: list[int] = []
    sold_candidates: list[int] = []

    direct_total = first_number_case_insensitive(basic, TOTAL_INVENTORY_KEYS)
    if direct_total is not None:
        total_candidates.append(direct_total)

    direct_sold = first_number_case_insensitive(basic, SOLD_INVENTORY_KEYS)
    if direct_sold is not None:
        sold_candidates.append(direct_sold)

    building_rows = list_rows_from_section(raw_json, ["GetBuildingDetails"])
    plot_rows = list_rows_from_section(raw_json, ["Tbl_Plots", "PlotDetails"])
    apartment_detail_rows = list_building_apartment_rows(raw_json)
    apartment_rows = list_rows_from_section(raw_json, ["GetApartmentAllotteeDetailsList"])

    building_total = sum_numbers_case_insensitive(building_rows, TOTAL_INVENTORY_KEYS)
    if building_total is not None:
        total_candidates.append(building_total)

    building_sold = sum_numbers_case_insensitive(building_rows, SOLD_INVENTORY_KEYS)
    if building_sold is not None:
        sold_candidates.append(building_sold)

    apartment_detail_total = sum_numbers_case_insensitive(apartment_detail_rows, ["NumberOfApartments"])
    if apartment_detail_total is not None:
        total_candidates.append(apartment_detail_total)

    apartment_detail_sold = sum_numbers_case_insensitive(apartment_detail_rows, ["NumberOfApartmentsBooked"])
    if apartment_detail_sold is not None:
        sold_candidates.append(apartment_detail_sold)

    # Fallback for plotted projects: count rows only when the table rows clearly look like plot rows.
    plot_total = count_inventory_rows(plot_rows, plot_mode=True)
    if plot_total is not None:
        total_candidates.append(plot_total)

    # Fallback for apartment/unit tables: count rows only when rows clearly look like individual units.
    unit_total = count_inventory_rows(apartment_rows, plot_mode=False)
    if unit_total is not None:
        total_candidates.append(unit_total)

    for rows in [plot_rows, apartment_rows, apartment_detail_rows, building_rows]:
        status_sold = count_sold_from_status_rows(rows)
        if status_sold is not None:
            sold_candidates.append(status_sold)

    total = max(total_candidates) if total_candidates else None
    sold = max(sold_candidates) if sold_candidates else None

    # If sold exceeds total, the source sections conflict. Hide sold instead of showing a wrong sold=total value.
    if sold is not None and total is not None and sold > total:
        sold = None

    unsold = None
    if sold is not None and total is not None:
        unsold = max(total - sold, 0)

    label = "NA"
    if sold is not None or total is not None:
        label = f"{sold if sold is not None else 'NA'} / {total if total is not None else 'NA'}"

    return {
        "sold": sold,
        "total": total,
        "unsold": unsold,
        "label": label,
    }


@st.cache_data(show_spinner=False, ttl=300)
def load_filter_options() -> dict[str, list[Any]]:
    connection = get_dashboard_connection()
    with connection.cursor() as cursor:
        districts = fetch_all(
            cursor,
            """
            SELECT DISTINCT district_name
            FROM rera_projects
            WHERE district_name IS NOT NULL AND district_name <> ''
            ORDER BY district_name
            """,
        )
        project_types = fetch_all(
            cursor,
            """
            SELECT DISTINCT project_type
            FROM rera_projects
            WHERE project_type IS NOT NULL AND project_type <> ''
            ORDER BY project_type
            """,
        )
        approved_years = fetch_all(
            cursor,
            """
            SELECT DISTINCT approved_year
            FROM rera_projects
            WHERE approved_year IS NOT NULL
            ORDER BY approved_year DESC
            """,
        )
    return {
        "districts": [row["district_name"] for row in districts],
        "project_types": [row["project_type"] for row in project_types],
        "approved_years": [row["approved_year"] for row in approved_years],
    }


def is_empty_value(value: Any) -> bool:
    return value in (None, "", [], {})


def unique_text_parts(parts: list[Any]) -> list[str]:
    seen: set[str] = set()
    unique_parts: list[str] = []
    for part in parts:
        text = clean_text(part)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique_parts.append(text)
    return unique_parts


def collect_professional_sections(raw_json: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    raw_json = raw_json or {}
    collected: dict[str, list[dict[str, Any]]] = {}

    nested = raw_json.get("ProjectProFessionAlDetail")
    if isinstance(nested, dict):
        for key, value in nested.items():
            if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                collected[key] = value

    for key in [
        "Architect",
        "Engineer",
        "NewEngineer",
        "CA",
        "ProjectAgent",
        "HVAC",
        "MEPConsultants",
        "Plumbing",
        "Other",
        "Contractor",
    ]:
        value = raw_json.get(key)
        if key not in collected and isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            collected[key] = value

    return collected


def collect_professional_names(raw_json: dict[str, Any] | None) -> list[str]:
    raw_json = raw_json or {}
    names: list[str] = []
    for items in collect_professional_sections(raw_json).values():
        for item in items:
            names.extend(
                unique_text_parts(
                    [
                        item.get("Name"),
                        item.get("PartnerName"),
                        item.get("OrgName"),
                    ]
                )
            )
    return unique_text_parts(names)


def default_district_selection(districts: list[str]) -> list[str]:
    for district in districts:
        if clean_text(district).casefold() == "jaipur":
            return [district]
    return []


@st.cache_data(show_spinner=False, ttl=300)
def load_project_indexes() -> dict[str, Any]:
    connection = get_dashboard_connection()
    with connection.cursor() as cursor:
        rows = fetch_all(
            cursor,
            """
            SELECT encrypted_project_id, raw_json
            FROM rera_projects
            WHERE raw_json IS NOT NULL
            """,
        )

    professional_names: set[str] = set()
    by_project: dict[str, dict[str, Any]] = {}
    for row in rows:
        names = collect_professional_names(row.get("raw_json"))
        professional_names.update(names)
        by_project[row["encrypted_project_id"]] = {
            "professional_names": names,
            "professional_name_keys": {
                text_key(name) for name in names if text_key(name)
            },
        }

    return {
        "professional_names": sorted(professional_names, key=str.casefold),
        "by_project": by_project,
    }


def build_project_query_conditions(
    *,
    search: str,
    districts: list[str],
    promoter_search: str,
    project_types: list[str],
    approved_years: list[int],
    changed_recently: bool,
    changed_days: int,
    only_with_raw_json: bool,
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if clean_text(search):
        pattern = f"%{search.strip()}%"
        conditions.append(
            """
            (
                project_name ILIKE %s
                OR registration_no ILIKE %s
                OR promoter_name ILIKE %s
                OR encrypted_project_id ILIKE %s
            )
            """
        )
        params.extend([pattern, pattern, pattern, pattern])

    if districts:
        conditions.append("district_name = ANY(%s)")
        params.append(districts)

    if clean_text(promoter_search):
        conditions.append("promoter_name ILIKE %s")
        params.append(f"%{promoter_search.strip()}%")

    if project_types:
        conditions.append("project_type = ANY(%s)")
        params.append(project_types)

    if approved_years:
        conditions.append("approved_year = ANY(%s)")
        params.append(approved_years)

    if only_with_raw_json:
        conditions.append("raw_json IS NOT NULL")

    if changed_recently:
        cutoff = datetime.now(timezone.utc) - timedelta(days=changed_days)
        conditions.append("last_changed_at IS NOT NULL AND last_changed_at >= %s")
        params.append(cutoff)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where_clause, params


STREET_NAME_SQL = """
COALESCE(
    NULLIF(TRIM(raw_json -> 'GetProjectBasic' ->> 'StreetName'), ''),
    NULLIF(TRIM(raw_json -> 'GetProjectBasic' ->> 'Street'), ''),
    NULLIF(TRIM(raw_json ->> 'StreetName'), ''),
    NULLIF(TRIM(raw_json ->> 'Street'), '')
)
"""


@st.cache_data(show_spinner=False, ttl=60)
def query_projects(
    *,
    search: str,
    districts: list[str],
    promoter_search: str,
    project_types: list[str],
    approved_years: list[int],
    changed_recently: bool,
    changed_days: int,
    only_with_raw_json: bool,
) -> list[dict[str, Any]]:
    connection = get_dashboard_connection()
    where_clause, params = build_project_query_conditions(
        search=search,
        districts=districts,
        promoter_search=promoter_search,
        project_types=project_types,
        approved_years=approved_years,
        changed_recently=changed_recently,
        changed_days=changed_days,
        only_with_raw_json=only_with_raw_json,
    )

    with connection.cursor() as cursor:
        rows = fetch_all(
            cursor,
            f"""
            SELECT
                id,
                encrypted_project_id,
                registration_no,
                project_name,
                district_name,
                tahsil_name,
                village_name,
                {STREET_NAME_SQL} AS street_name,
                plot_no,
                promoter_name,
                project_type,
                approved_year,
                project_status,
                area_sqm,
                phase_area_sqm,
                saleable_area_sqm,
                total_building_count,
                sanctioned_building_count,
                not_sanctioned_building_count,
                last_changed_at,
                raw_json IS NOT NULL AS has_raw_json
            FROM rera_projects
            {where_clause}
            ORDER BY
                CASE
                    WHEN registration_no ILIKE '%%RERAExempted%%' THEN 1
                    ELSE 0
                END ASC,
                COALESCE(((regexp_match(registration_no, '/([0-9]{{4}})/([0-9]+)(?:-[A-Za-z0-9]+)?$'))[1])::int, 0) DESC,
                COALESCE(((regexp_match(registration_no, '/([0-9]{{4}})/([0-9]+)(?:-[A-Za-z0-9]+)?$'))[2])::int, 0) DESC,
                COALESCE(last_changed_at, last_scraped_at, created_at) DESC NULLS LAST,
                id DESC
            """,
            params,
        )
    return rows


@st.cache_data(show_spinner=False, ttl=60)
def query_project_stats(
    *,
    search: str,
    districts: list[str],
    promoter_search: str,
    project_types: list[str],
    approved_years: list[int],
    changed_recently: bool,
    changed_days: int,
    only_with_raw_json: bool,
) -> dict[str, int]:
    connection = get_dashboard_connection()
    where_clause, params = build_project_query_conditions(
        search=search,
        districts=districts,
        promoter_search=promoter_search,
        project_types=project_types,
        approved_years=approved_years,
        changed_recently=changed_recently,
        changed_days=changed_days,
        only_with_raw_json=only_with_raw_json,
    )
    with connection.cursor() as cursor:
        row = fetch_one(
            cursor,
            f"""
            SELECT
                COUNT(*)::int AS filtered_projects,
                COUNT(*) FILTER (WHERE raw_json IS NOT NULL)::int AS with_raw_json,
                COUNT(*) FILTER (WHERE last_changed_at IS NOT NULL)::int AS with_tracked_changes
            FROM rera_projects
            {where_clause}
            """,
            params,
        )
    return row or {
        "filtered_projects": 0,
        "with_raw_json": 0,
        "with_tracked_changes": 0,
    }


def query_projects_page(
    *,
    search: str,
    districts: list[str],
    promoter_search: str,
    project_types: list[str],
    approved_years: list[int],
    changed_recently: bool,
    changed_days: int,
    only_with_raw_json: bool,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    return fetch_projects_page_sql(
        search=search,
        districts=districts,
        promoter_search=promoter_search,
        project_types=project_types,
        approved_years=approved_years,
        changed_recently=changed_recently,
        changed_days=changed_days,
        only_with_raw_json=only_with_raw_json,
        limit=limit,
        offset=offset,
    )


def fetch_projects_page_sql(
    *,
    search: str,
    districts: list[str],
    promoter_search: str,
    project_types: list[str],
    approved_years: list[int],
    changed_recently: bool,
    changed_days: int,
    only_with_raw_json: bool,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    connection = get_dashboard_connection()
    where_clause, params = build_project_query_conditions(
        search=search,
        districts=districts,
        promoter_search=promoter_search,
        project_types=project_types,
        approved_years=approved_years,
        changed_recently=changed_recently,
        changed_days=changed_days,
        only_with_raw_json=only_with_raw_json,
    )

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                id,
                encrypted_project_id,
                registration_no,
                project_name,
                district_name,
                tahsil_name,
                village_name,
                {STREET_NAME_SQL} AS street_name,
                plot_no,
                promoter_name,
                project_type,
                approved_year,
                project_status,
                area_sqm,
                phase_area_sqm,
                saleable_area_sqm,
                total_building_count,
                sanctioned_building_count,
                not_sanctioned_building_count,
                last_changed_at,
                raw_json IS NOT NULL AS has_raw_json
            FROM rera_projects
            {where_clause}
            ORDER BY
                CASE
                    WHEN registration_no ILIKE '%%RERAExempted%%' THEN 1
                    ELSE 0
                END ASC,
                COALESCE(((regexp_match(registration_no, '/([0-9]{{4}})/([0-9]+)(?:-[A-Za-z0-9]+)?$'))[1])::int, 0) DESC,
                COALESCE(((regexp_match(registration_no, '/([0-9]{{4}})/([0-9]+)(?:-[A-Za-z0-9]+)?$'))[2])::int, 0) DESC,
                COALESCE(last_changed_at, last_scraped_at, created_at) DESC NULLS LAST,
                id DESC
            LIMIT %s
            OFFSET %s
            """,
            tuple([*params, limit, offset]),
        )
        rows = cursor.fetchall()
    return rows


@st.cache_data(show_spinner=False, ttl=300)
def load_inventory_for_projects(project_ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if not project_ids:
        return {}

    connection = get_dashboard_connection()
    with connection.cursor() as cursor:
        rows = fetch_all(
            cursor,
            """
            SELECT encrypted_project_id, raw_json
            FROM rera_projects
            WHERE encrypted_project_id = ANY(%s)
              AND raw_json IS NOT NULL
            """,
            (list(project_ids),),
        )

    inventory_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        encrypted_project_id = row.get("encrypted_project_id")
        if not encrypted_project_id:
            continue
        inventory_by_id[encrypted_project_id] = get_inventory_summary({"raw_json": row.get("raw_json")})
    return inventory_by_id


@st.cache_data(show_spinner=False, ttl=60)
def load_project(encrypted_project_id: str) -> dict[str, Any] | None:
    connection = get_dashboard_connection()
    with connection.cursor() as cursor:
        return fetch_one(
            cursor,
            """
            SELECT *
            FROM rera_projects
            WHERE encrypted_project_id = %s
            LIMIT 1
            """,
            (encrypted_project_id,),
        )


@st.cache_data(show_spinner=False, ttl=60)
def load_project_changes(encrypted_project_id: str) -> list[dict[str, Any]]:
    connection = get_dashboard_connection()
    with connection.cursor() as cursor:
        return fetch_all(
            cursor,
            """
            SELECT
                field_path,
                change_type,
                old_value,
                new_value,
                changed_at
            FROM rera_project_changes
            WHERE encrypted_project_id = %s
            ORDER BY changed_at DESC, id DESC
            """,
            (encrypted_project_id,),
        )


def build_project_detail_url(encrypted_project_id: str) -> str:
    return f"?project={encrypted_project_id}"


def format_project_option(
    encrypted_project_id: str,
    projects_by_id: dict[str, dict[str, Any]],
) -> str:
    project = projects_by_id[encrypted_project_id]
    return (
        f"{project.get('project_name') or 'Unnamed'} | "
        f"{project.get('registration_no') or encrypted_project_id}"
    )


def render_section_table(title: str, data: dict[str, Any] | None) -> None:
    if not data:
        st.info(f"No {title.lower()} available.")
        return
    rows = [
        {"Field": key, "Value": normalize_table_value(value)}
        for key, value in data.items()
    ]
    st.markdown(f"**{title}**")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def render_list_table(title: str, items: list[Any]) -> None:
    if not items:
        return
    st.markdown(f"**{title}**")
    if all(isinstance(item, dict) for item in items):
        display_rows = [
            {key: normalize_table_value(value) for key, value in item.items()}
            for item in items[:200]
        ]
        st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
        if len(items) > 200:
            st.caption(f"Showing first 200 of {len(items)} rows.")
    else:
        st.dataframe(
            pd.DataFrame({"Value": [normalize_table_value(item) for item in items[:200]]}),
            use_container_width=True,
            hide_index=True,
        )
        if len(items) > 200:
            st.caption(f"Showing first 200 of {len(items)} rows.")


def render_nested_json_section(title: str, value: Any, *, depth: int = 0) -> None:
    if is_empty_value(value):
        return

    if isinstance(value, dict):
        scalar_items = {
            key: item
            for key, item in value.items()
            if not is_empty_value(item) and not isinstance(item, (dict, list))
        }
        nested_dicts = {
            key: item
            for key, item in value.items()
            if isinstance(item, dict) and item
        }
        nested_lists = {
            key: item
            for key, item in value.items()
            if isinstance(item, list) and item
        }

        if scalar_items:
            render_section_table(title, scalar_items)
        elif depth == 0:
            st.markdown(f"**{title}**")

        for key, item in nested_dicts.items():
            render_nested_json_section(
                f"{title} / {humanize_identifier(key)}",
                item,
                depth=depth + 1,
            )
        for key, item in nested_lists.items():
            render_list_table(f"{title} / {humanize_identifier(key)}", item)
        return

    if isinstance(value, list):
        render_list_table(title, value)
        return

    render_section_table(title, {"Value": value})


def unique_lon_lat_points(points: list[dict[str, Any]]) -> list[list[float]]:
    seen: set[tuple[float, float]] = set()
    unique: list[list[float]] = []
    for point in points:
        try:
            lon = round(float(point["lon"]), 7)
            lat = round(float(point["lat"]), 7)
        except (KeyError, TypeError, ValueError):
            continue
        key = (lon, lat)
        if key in seen:
            continue
        seen.add(key)
        unique.append([lon, lat])
    return unique


def convex_hull(points: list[list[float]]) -> list[list[float]]:
    points = sorted(points)
    if len(points) <= 2:
        return points

    def cross(o: list[float], a: list[float], b: list[float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[list[float]] = []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)

    upper: list[list[float]] = []
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)

    hull = lower[:-1] + upper[:-1]
    if hull and hull[0] != hull[-1]:
        hull.append(hull[0])
    return hull


def render_pydeck_location_map(
    *,
    latitude: float,
    longitude: float,
    points: list[dict[str, Any]] | None = None,
    paths: list[dict[str, Any]] | None = None,
    polygons: list[dict[str, Any]] | None = None,
    map_style_name: str = "Detailed",
    zoom: int = 14,
) -> None:
    layers: list[pdk.Layer] = []

    if polygons:
        layers.append(
            pdk.Layer(
                "PolygonLayer",
                data=polygons,
                get_polygon="polygon",
                get_fill_color=[123, 94, 59, 55],
                get_line_color=[123, 94, 59, 220],
                get_line_width=3,
                stroked=True,
                filled=True,
                pickable=True,
            )
        )

    if paths:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=paths,
                get_path="path",
                get_width=5,
                get_color=[87, 113, 95, 220],
                width_min_pixels=2,
                pickable=True,
            )
        )

    if points:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=points,
                get_position="[lon, lat]",
                get_radius=14,
                get_fill_color=[220, 78, 65, 220],
                radius_min_pixels=4,
                pickable=True,
            )
        )

    view_state = pdk.ViewState(
        latitude=float(latitude),
        longitude=float(longitude),
        zoom=zoom,
        pitch=0,
    )
    st.pydeck_chart(
        pdk.Deck(
            map_style=MAP_STYLE_OPTIONS.get(map_style_name, DETAILED_MAP_STYLE_URL),
            initial_view_state=view_state,
            layers=layers,
            tooltip={"text": "{name}"},
        ),
        use_container_width=True,
    )


def estimate_zoom_from_bounds(bounds: dict[str, Any] | None) -> int:
    if not bounds:
        return 15
    try:
        lat_span = abs(float(bounds["max_lat"]) - float(bounds["min_lat"]))
        lon_span = abs(float(bounds["max_lon"]) - float(bounds["min_lon"]))
    except Exception:
        return 15

    span = max(lat_span, lon_span)
    if span <= 0.002:
        return 17
    if span <= 0.005:
        return 16
    if span <= 0.015:
        return 15
    if span <= 0.05:
        return 14
    if span <= 0.15:
        return 13
    if span <= 0.4:
        return 12
    return 11


def render_map_document(document: dict[str, Any], *, map_style_name: str = "Detailed") -> bool:
    try:
        parsed = parse_map_document(document["url"])
    except Exception as exc:  # noqa: BLE001
        st.error(f"Map parsing failed: {exc}")
        return False

    features = parsed.get("features") or []
    all_points = flatten_geometry_points(features)
    if not all_points:
        st.info("No coordinates found in the selected map document.")
        return False

    scatter_rows = [
        {"name": "Coordinate", "lat": point["lat"], "lon": point["lon"]}
        for point in all_points
    ]
    path_rows: list[dict[str, Any]] = []
    polygon_rows: list[dict[str, Any]] = []

    for feature in features:
        geometry_type = feature.get("geometry_type")
        if geometry_type == "LineString":
            path_rows.append(
                {
                    "name": feature.get("name") or "Boundary",
                    "path": [[point["lon"], point["lat"]] for point in feature.get("coordinates", [])],
                }
            )
        elif geometry_type == "Polygon":
            for ring in feature.get("rings", []):
                polygon_rows.append(
                    {
                        "name": feature.get("name") or "Project polygon",
                        "polygon": [[point["lon"], point["lat"]] for point in ring],
                    }
                )

    # Some KML/KMZ files expose only coordinate points/paths. Build a fallback convex-hull polygon.
    if not polygon_rows:
        unique_points = unique_lon_lat_points(all_points)
        if len(unique_points) >= 3:
            hull = convex_hull(unique_points)
            if len(hull) >= 4:
                polygon_rows.append({"name": "Approximate project boundary", "polygon": hull})
                path_rows.append({"name": "Approximate boundary line", "path": hull})

    center = parsed.get("center") or {"lat": all_points[0]["lat"], "lon": all_points[0]["lon"]}
    render_pydeck_location_map(
        latitude=float(center["lat"]),
        longitude=float(center["lon"]),
        points=scatter_rows,
        paths=path_rows,
        polygons=polygon_rows,
        map_style_name=map_style_name,
        zoom=estimate_zoom_from_bounds(parsed.get("bounds")),
    )

    st.caption(
        f"Geometry count: {parsed.get('geometry_count', 0)} | "
        f"Coordinate count: {parsed.get('coordinate_count', 0)} | "
        f"Polygon count: {len(polygon_rows)} | "
        f"View: {map_style_name}"
    )

    with st.expander("Show extracted coordinates", expanded=False):
        st.dataframe(pd.DataFrame(scatter_rows), use_container_width=True, hide_index=True)

    return True


def collect_address_parts_from_raw_json(raw_json: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    likely_keys = [
        "ProjectAddress", "ProjectLocation", "Address", "Location", "Street", "StreetName",
        "Landmark", "Locality", "Area", "VillageName", "TahsilName", "DistrictName",
    ]
    for data in iter_dicts(raw_json):
        for key in likely_keys:
            value = data.get(key)
            text = clean_text(value)
            if text and len(text) <= 200 and not text.isdigit():
                parts.append(text)
    return unique_text_parts(parts)


def build_geocode_query(project: dict[str, Any]) -> str | None:
    raw_json = project.get("raw_json") or {}
    if not isinstance(raw_json, dict):
        raw_json = {}

    parts = unique_text_parts(
        [
            project.get("plot_no"),
            project.get("village_name"),
            display_tehsil(project.get("tahsil_name")),
            project.get("district_name"),
            *collect_address_parts_from_raw_json(raw_json),
            "Rajasthan",
            "India",
        ]
    )
    query = ", ".join(part for part in parts if part and part != "NA")
    return query or None


@st.cache_data(show_spinner=False, ttl=86400)
def geocode_address(query: str, endpoint: str, user_agent: str, email: str | None) -> dict[str, Any] | None:
    headers = {"User-Agent": user_agent or "rera-rajasthan-intel/1.0"}
    params: dict[str, Any] = {
        "q": query,
        "format": "json",
        "limit": 1,
        "polygon_geojson": 1,
        "addressdetails": 1,
    }
    if email:
        params["email"] = email

    response = requests.get(endpoint, params=params, headers=headers, timeout=(10, 30))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        return None
    return payload[0]


def render_geocoded_project_map(project: dict[str, Any]) -> None:
    settings = get_settings()
    query = build_geocode_query(project)
    if not query:
        st.info("No map document or usable address was found for geocoding.")
        return

    try:
        result = geocode_address(
            query,
            settings.geocoding_endpoint,
            settings.geocoding_user_agent,
            settings.geocoding_email,
        )
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Address geocoding failed for: {query}. Error: {exc}")
        return

    if not result:
        st.info(f"No geocoding result found for: {query}")
        return

    lat = number_or_none(result.get("lat"))
    lon = number_or_none(result.get("lon"))
    if lat is None or lon is None:
        st.info(f"Geocoding result did not contain coordinates for: {query}")
        return

    point = {
        "name": project.get("project_name") or result.get("display_name") or "Project location",
        "lat": lat,
        "lon": lon,
    }

    st.caption(f"Location estimated from address: {query}")
    render_pydeck_location_map(latitude=lat, longitude=lon, points=[point], zoom=15)
    st.caption(f"Matched address: {result.get('display_name') or 'NA'}")


def render_project_overview(project: dict[str, Any]) -> None:
    raw_json = project.get("raw_json") or {}
    if not isinstance(raw_json, dict):
        raw_json = {}

    inventory = get_inventory_summary(project)
    registration_no = display_text(project.get("registration_no"))

    st.markdown(f"**Registration No:** `{registration_no}`")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("District", display_text(project.get("district_name")))
    col2.metric("Approved Year", display_text(project.get("approved_year")))
    col3.metric("Project Type", display_text(project.get("project_type")))
    col4.metric("Status", display_text(project.get("project_status")))

    area_col1, area_col2, area_col3, area_col4 = st.columns(4)
    area_col1.metric("Project Area", area_display(project.get("area_sqm")))
    area_col2.metric("Phase Area", area_display(project.get("phase_area_sqm")))
    area_col3.metric("BUA / Saleable Area", area_display(project.get("saleable_area_sqm")))
    area_col4.metric("Sold / Total", inventory["label"])

    inv_col1, inv_col2, inv_col3 = st.columns(3)
    inv_col1.metric("Sold / Allotted", int_display(inventory.get("sold")))
    inv_col2.metric("Total Units / Plots / Flats", int_display(inventory.get("total")))
    inv_col3.metric("Unsold", int_display(inventory.get("unsold")))

    overview_rows = [
        {"Field": "Project Name", "Value": project.get("project_name")},
        {"Field": "Promoter", "Value": project.get("promoter_name")},
        {"Field": "Project Type", "Value": project.get("project_type")},
        {"Field": "Project Status", "Value": project.get("project_status")},
        {"Field": "Registration No", "Value": registration_no},
        {"Field": "District", "Value": project.get("district_name")},
        {"Field": "Area / Village", "Value": project.get("village_name")},
        {"Field": "Plot No", "Value": project.get("plot_no")},
        {"Field": "Tehsil", "Value": display_tehsil(project.get("tahsil_name"))},
        {"Field": "Project Area", "Value": area_display(project.get("area_sqm"))},
        {"Field": "Phase Area", "Value": area_display(project.get("phase_area_sqm"))},
        {"Field": "BUA / Saleable Area", "Value": area_display(project.get("saleable_area_sqm"))},
        {"Field": "Total Units / Plots / Flats", "Value": int_display(inventory.get("total"))},
        {"Field": "Sold / Allotted", "Value": int_display(inventory.get("sold"))},
        {"Field": "Unsold", "Value": int_display(inventory.get("unsold"))},
        {"Field": "Sold / Total Inventory", "Value": inventory["label"]},
    ]

    st.markdown("**Project Overview Details**")
    st.dataframe(
        pd.DataFrame(overview_rows),
        use_container_width=True,
        hide_index=True,
    )

    documents = collect_project_documents(
        project.get("raw_json"),
        project.get("source_csv_row"),
    )
    map_documents = collect_map_documents(documents)

    st.markdown("**Map / Location**")
    rendered_map = False
    if map_documents:
        map_style_name = st.segmented_control(
            "Map view",
            options=["Detailed", "Hybrid", "Satellite"],
            default="Detailed",
            help="Detailed shows roads and locality labels, Hybrid mixes imagery with transport/label overlays, and Satellite shows imagery-first context.",
        )
        options = {f"{doc['title']} | {doc['file_name']}": doc for doc in map_documents}
        selected_label = st.selectbox("Map document", options=list(options.keys()))
        rendered_map = render_map_document(options[selected_label], map_style_name=map_style_name)

    if not rendered_map:
        st.info("No usable KML/KMZ polygon was found. Trying address-based geocoding.")
        render_geocoded_project_map(project)

    st.markdown("**Inventory & Area Detail Sections**")
    important_sections = [
        "GetProjectBasic",
        "GetBuildingDetails",
        "GetApartmentAllotteeDetailsList",
        "Tbl_Plots",
        "PlotDetails",
        "GetProjectAreaFacilities",
        "ProjectCommanArea",
        "ProjectSummaryPayment",
        "GetProjectCostDetail",
    ]

    rendered_any = False
    for section in important_sections:
        value = raw_json.get(section)
        if is_empty_value(value):
            continue

        rendered_any = True
        with st.expander(
            humanize_identifier(section),
            expanded=section in {"GetProjectBasic", "GetBuildingDetails", "Tbl_Plots", "PlotDetails"},
        ):
            render_nested_json_section(humanize_identifier(section), value)

    if not rendered_any:
        st.info("No inventory or area detail sections found in raw JSON.")


def render_price_candidates_tab(project: dict[str, Any]) -> None:
    connection = get_dashboard_connection()
    settings = get_settings()
    price_candidates = load_project_price_candidates(connection, project["id"])
    manual_prices = load_project_market_prices(connection, project["id"])
    resolved = resolve_selected_market_price(
        price_candidates=price_candidates,
        manual_prices=manual_prices,
    )

    if resolved:
        st.success(
            f"{resolved.get('label')}: {resolved.get('source') or 'NA'} | "
            f"Price: {format_currency(resolved.get('price'))} | "
            f"Price/sqft: {format_currency(resolved.get('price_per_sqft'))}"
        )
    else:
        st.info("No selected market price available yet.")

    if settings.serpapi_key:
        if st.button("Refresh prices for this project", key=f"refresh-prices::{project['id']}"):
            with st.spinner("Refreshing price candidates..."):
                refresh_project_price_candidates(
                    connection,
                    project=project,
                    api_key=settings.serpapi_key,
                    gl=settings.serpapi_gl,
                    hl=settings.serpapi_hl,
                    default_location=settings.serpapi_location,
                )
            st.rerun()
    else:
        st.caption("Set `SERPAPI_KEY` in local env or Streamlit secrets to enable automated price discovery.")

    if price_candidates:
        display_rows = [
            {
                "source": row.get("source"),
                "result_title": row.get("result_title"),
                "price": row.get("extracted_price_value"),
                "price_per_sqft": row.get("price_per_sqft"),
                "confidence_score": row.get("confidence_score"),
                "source_url": row.get("source_url"),
                "created_at": row.get("created_at"),
            }
            for row in price_candidates
        ]
        st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
    else:
        st.info("No automated price candidates found yet.")


def render_manual_prices_tab(project: dict[str, Any]) -> None:
    connection = get_dashboard_connection()
    manual_prices = load_project_market_prices(connection, project["id"])
    if manual_prices:
        st.dataframe(pd.DataFrame(manual_prices), use_container_width=True, hide_index=True)
    else:
        st.info("No manual prices recorded yet.")

    with st.form(key=f"manual-price-form::{project['id']}"):
        source = st.text_input("Source", value="Manual comparable")
        source_url = st.text_input("Source URL")
        listing_title = st.text_input("Listing title")
        price = st.number_input("Price", min_value=0.0, value=0.0, step=100000.0)
        area = st.number_input("Area (sqft)", min_value=0.0, value=0.0, step=100.0)
        notes = st.text_area("Notes")
        submitted = st.form_submit_button("Save manual market price")

    if submitted:
        price_value = float(price) if price > 0 else None
        area_value = float(area) if area > 0 else None
        insert_project_market_price(
            connection,
            project_id=project["id"],
            encrypted_project_id=project["encrypted_project_id"],
            source=source.strip() or "Manual comparable",
            source_url=source_url.strip() or None,
            listing_title=listing_title.strip() or None,
            price=price_value,
            area=area_value,
            price_per_sqft=calculate_price_per_sqft(price=price_value, area=area_value),
            notes=notes.strip() or None,
            raw_data={"captured_via": "streamlit_dashboard"},
        )
        st.success("Manual market price saved.")
        st.rerun()


def render_roi_tab(project: dict[str, Any]) -> None:
    connection = get_dashboard_connection()
    roi_cases = load_project_roi_cases(connection, project["id"])
    if roi_cases:
        st.dataframe(pd.DataFrame(roi_cases), use_container_width=True, hide_index=True)
    else:
        st.info("No saved ROI scenarios yet.")

    with st.form(key=f"roi-form::{project['id']}"):
        scenario_name = st.text_input("Scenario name")
        purchase_price = st.number_input("Purchase price", min_value=0.0, value=5000000.0, step=100000.0)
        stamp_duty = st.number_input("Stamp duty", min_value=0.0, value=0.0, step=10000.0)
        registration = st.number_input("Registration", min_value=0.0, value=0.0, step=10000.0)
        brokerage = st.number_input("Brokerage", min_value=0.0, value=0.0, step=10000.0)
        other_cost = st.number_input("Other cost", min_value=0.0, value=0.0, step=10000.0)
        expected_sale_price = st.number_input(
            "Expected sale price",
            min_value=0.0,
            value=6500000.0,
            step=100000.0,
        )
        holding_period_months = st.number_input("Holding period (months)", min_value=1, value=24, step=1)
        submitted = st.form_submit_button("Calculate and save ROI scenario")

    if submitted:
        _case_id, metrics = insert_project_roi_case(
            connection,
            project_id=project["id"],
            encrypted_project_id=project["encrypted_project_id"],
            scenario_name=scenario_name.strip() or None,
            purchase_price=float(purchase_price),
            stamp_duty=float(stamp_duty),
            registration=float(registration),
            brokerage=float(brokerage),
            other_cost=float(other_cost),
            expected_sale_price=float(expected_sale_price),
            holding_period_months=int(holding_period_months),
        )
        st.success(
            f"ROI saved. ROI: {format_percent(metrics.get('roi_pct'))} | "
            f"Annualized ROI: {format_percent(metrics.get('annualized_roi_pct'))}"
        )
        st.rerun()


def render_sync_tab() -> None:
    settings = get_settings()
    if not settings.serpapi_key:
        st.caption("Set `SERPAPI_KEY` in local env or Streamlit secrets to enable weekly price sync.")
        return
    limit = st.number_input("Weekly sync project limit", min_value=1, value=20, step=1)
    if st.button("Run weekly price sync"):
        with st.spinner("Running price sync..."):
            stats = run_weekly_price_sync(
                get_dashboard_connection(),
                api_key=settings.serpapi_key,
                gl=settings.serpapi_gl,
                hl=settings.serpapi_hl,
                default_location=settings.serpapi_location,
                limit=int(limit),
            )
        st.success("Weekly price sync completed.")
        st.json(stats)


def render_market_pricing_tab(project: dict[str, Any]) -> None:
    prices_subtab, manual_subtab, roi_subtab, sync_subtab = st.tabs(
        ["Auto Candidates", "Manual Prices", "ROI Calculator", "Sync"]
    )
    with prices_subtab:
        render_price_candidates_tab(project)
    with manual_subtab:
        render_manual_prices_tab(project)
    with roi_subtab:
        render_roi_tab(project)
    with sync_subtab:
        render_sync_tab()


def render_professionals_tab(project: dict[str, Any]) -> None:
    raw_json = project.get("raw_json") or {}
    rendered = False

    promoter_details = raw_json.get("PromoterDetails")
    if isinstance(promoter_details, dict) and promoter_details:
        rendered = True
        promoter_summary = {
            key: promoter_details.get(key)
            for key in [
                "OrgName",
                "OrgType",
                "FirstName",
                "MiddleName",
                "LastName",
                "FatherName",
                "MobileNo",
                "OfficeNo",
                "LandlineNumber",
                "WebSiteURL",
            ]
            if not is_empty_value(promoter_details.get(key))
        }
        if promoter_summary:
            render_section_table("Promoter Details", promoter_summary)
        if isinstance(promoter_details.get("Address"), dict) and promoter_details["Address"]:
            render_section_table("Promoter Address", promoter_details["Address"])
        if isinstance(promoter_details.get("PartnerModel"), list) and promoter_details["PartnerModel"]:
            render_list_table("Promoter Partners", promoter_details["PartnerModel"])
        if isinstance(promoter_details.get("PastExprienceDetails"), list) and promoter_details["PastExprienceDetails"]:
            render_list_table("Past Experience", promoter_details["PastExprienceDetails"])

    professional_sections = collect_professional_sections(raw_json)
    preferred_columns = [
        "Name",
        "Type",
        "ContactNumber",
        "Email",
        "RegistrationNo",
        "COARegistrationNo",
        "Address",
        "IsActive",
        "CreatedOn",
        "UpdatedOn",
    ]
    for section, rows in professional_sections.items():
        if not rows:
            continue
        rendered = True
        display_rows = []
        for row in rows:
            display_row = {
                column: normalize_table_value(row.get(column))
                for column in preferred_columns
                if not is_empty_value(row.get(column))
            }
            if not display_row:
                display_row = {
                    key: normalize_table_value(value)
                    for key, value in row.items()
                    if not is_empty_value(value)
                }
            display_rows.append(display_row)
        st.markdown(f"**{humanize_identifier(section)}**")
        st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)

    professional_detail_meta = raw_json.get("ProjectProFessionAlDetail")
    if isinstance(professional_detail_meta, dict):
        remaining_meta = {
            key: value
            for key, value in professional_detail_meta.items()
            if key not in professional_sections
            and not isinstance(value, (dict, list))
            and not is_empty_value(value)
        }
        if remaining_meta:
            rendered = True
            render_section_table("Professional Metadata", remaining_meta)

    if not rendered:
        st.info("No professional sections found in the raw JSON.")


def render_structured_highlights(project: dict[str, Any]) -> None:
    raw_json = project.get("raw_json") or {}
    sections = [
        "GetProjectBasic",
        "GetProjectAreaFacilities",
        "GetProjectCostDetail",
        "GetBuildingDetails",
        "ProjectCommanArea",
        "ProjectSummaryPayment",
        "PlotDetails",
        "Tbl_Plots",
        "Sanctioned_Notsanctioned",
        "CommonAreaItemsCharged",
        "GetApartmentAllotteeDetailsList",
        "GetPreviousExtsList",
        "_ProposedEncumbrance",
        "ReasonofExemptionSummary",
        "ProjectLitigations",
    ]
    rendered = False
    for section in sections:
        value = raw_json.get(section)
        if is_empty_value(value):
            continue
        rendered = True
        with st.expander(humanize_identifier(section), expanded=section == "GetProjectBasic"):
            render_nested_json_section(humanize_identifier(section), value)

    additional_sections = [
        key
        for key, value in raw_json.items()
        if key not in sections
        and key
        not in {
            "PromoterDetails",
            "ProjectProFessionAlDetail",
            "ProjectDocuments",
            "GetDocumentsList",
            "PromoterDocumentList",
            "Architect",
            "Engineer",
            "NewEngineer",
            "CA",
            "ProjectAgent",
            "HVAC",
            "MEPConsultants",
            "Plumbing",
            "Other",
            "Contractor",
        }
        and not is_empty_value(value)
    ]
    for section in sorted(additional_sections):
        rendered = True
        with st.expander(f"Additional / {humanize_identifier(section)}", expanded=False):
            render_nested_json_section(humanize_identifier(section), raw_json.get(section))
    if not rendered:
        st.info("No structured highlights available.")


def normalize_url(url: Any) -> str:
    text = clean_text(url) or ""
    if text.startswith("//"):
        return f"https:{text}"
    return text


def render_documents_tab(project: dict[str, Any]) -> None:
    settings = get_settings()
    documents = collect_project_documents(
        project.get("raw_json"),
        project.get("source_csv_row"),
    )
    if not documents:
        st.info("No project documents found.")
        return

    display_rows = []
    for row in documents:
        url = normalize_url(row.get("url"))
        display_rows.append(
            {
                "section": row.get("section"),
                "title": row.get("title"),
                "file_name": row.get("file_name"),
                "document_type": row.get("document_type"),
                "document_kind": row.get("document_kind"),
                "Open": url,
                "URL": url,
            }
        )

    st.dataframe(
        pd.DataFrame(display_rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Open": st.column_config.LinkColumn(
                "Open",
                display_text="Open document",
            )
        },
    )

    options = {f"{row['title']} | {row['file_name']}": row for row in documents}
    selected_label = st.selectbox("Select document", options=list(options.keys()))
    selected = options[selected_label]
    selected_url = normalize_url(selected.get("url"))

    if selected_url:
        st.markdown(f"[Open selected document]({selected_url})")
        st.link_button("Open selected document", selected_url)
        st.code(selected_url, language=None)
    else:
        st.warning("This document has no usable URL.")

    if selected.get("document_kind") == "image" and selected_url:
        st.image(selected_url, caption=selected.get("title"))

    if selected_url:
        try:
            probe = probe_document(selected_url)
            st.caption(
                f"Content-Type: {probe.get('content_type') or 'unknown'} | "
                f"Size: {probe.get('content_length') or 'unknown'} bytes"
            )
        except Exception as exc:  # noqa: BLE001
            st.caption(f"Document probe failed: {exc}")

    if not settings.openai_api_key:
        st.info(
            "No OpenAI key detected yet. Add `OPENAI_API_KEY` to local `.env` "
            "or Streamlit Cloud secrets, then rerun the page."
        )
        return

    st.caption(f"OpenAI summary model: `{settings.openai_summary_model}`")
    if selected_url and st.button("Summarize with OpenAI", key=f"summarize::{selected_url}"):
        try:
            with st.spinner("Reading document with OpenAI..."):
                summary = summarize_remote_document(
                    url=selected_url,
                    title=selected.get("title") or "Selected document",
                    api_key=settings.openai_api_key,
                    model=settings.openai_summary_model,
                )
            st.markdown("**OpenAI Summary**")
            st.write(summary)
        except Exception as exc:  # noqa: BLE001
            st.error(f"OpenAI summary failed: {exc}")


def render_raw_json_explorer(project: dict[str, Any]) -> None:
    st.json(project.get("raw_json") or {})



# -----------------------------------------------------------------------------
# AI research chat: database + live internet through OpenAI Responses API
# -----------------------------------------------------------------------------

OPENAI_RESPONSES_API_URL = "https://api.openai.com/v1/responses"
DEFAULT_AI_RESEARCH_MODEL = "gpt-5.5"
AI_CHAT_MAX_ROWS = 200
AI_CHAT_STATEMENT_TIMEOUT_MS = 15000
AI_CHAT_ALLOWED_TABLE_PREFIXES = (
    "rera_",
    "project_",
)
AI_CHAT_EXPLICIT_ALLOWED_TABLES = {
    "rera_projects",
    "rera_project_changes",
    "rera_project_snapshots",
    "project_market_prices",
    "project_roi_cases",
    "project_price_candidates",
}
AI_CHAT_DISALLOWED_SQL_WORDS = {
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "truncate",
    "grant",
    "revoke",
    "copy",
    "call",
    "do",
    "execute",
    "vacuum",
    "analyze",
    "refresh",
    "reindex",
    "cluster",
    "attach",
    "detach",
    "listen",
    "notify",
    "unlisten",
    "pg_sleep",
    "dblink",
    "lo_import",
    "lo_export",
}


def get_ai_research_model() -> str:
    settings = get_settings()
    return (
        os.getenv("OPENAI_RESEARCH_MODEL", "").strip()
        or os.getenv("OPENAI_DB_CHAT_MODEL", "").strip()
        or os.getenv("OPENAI_SUMMARY_MODEL", "").strip()
        or getattr(settings, "openai_summary_model", "")
        or DEFAULT_AI_RESEARCH_MODEL
    )


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


@st.cache_data(show_spinner=False, ttl=600)
def load_ai_database_schema_summary() -> str:
    connection = get_dashboard_connection()
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
        table_rows = cursor.fetchall()

        table_names: list[str] = []
        for row in table_rows:
            table_name = row.get("table_name") if isinstance(row, dict) else row[0]
            if table_name in AI_CHAT_EXPLICIT_ALLOWED_TABLES or table_name.startswith(AI_CHAT_ALLOWED_TABLE_PREFIXES):
                table_names.append(table_name)

        if not table_names:
            return "No readable public tables found."

        cursor.execute(
            """
            SELECT
                table_name,
                column_name,
                data_type,
                ordinal_position
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
            ORDER BY table_name, ordinal_position
            """,
            (table_names,),
        )
        column_rows = cursor.fetchall()

    by_table: dict[str, list[str]] = {table: [] for table in table_names}
    for row in column_rows:
        table_name = row.get("table_name") if isinstance(row, dict) else row[0]
        column_name = row.get("column_name") if isinstance(row, dict) else row[1]
        data_type = row.get("data_type") if isinstance(row, dict) else row[2]
        by_table.setdefault(table_name, []).append(f"{column_name} ({data_type})")

    lines: list[str] = []
    for table_name in table_names:
        columns = by_table.get(table_name, [])
        if not columns:
            continue
        lines.append(f"Table: {table_name}")
        lines.append("Columns: " + ", ".join(columns))

    lines.append(
        "Important notes: rera_projects is the primary project table. "
        "A Jaipur micro-market should usually be estimated from village_name first, then tahsil_name, then district_name. "
        "Use district_name ILIKE '%jaipur%' for Jaipur. "
        "Use approved_year as RERA launch/approval timing. "
        "Use project_type for plotted/group housing/commercial breakdowns. "
        "Use area_sqm, phase_area_sqm, saleable_area_sqm for project size. "
        "Use raw_json IS NOT NULL as detail availability, and COALESCE(last_changed_at,last_scraped_at,created_at) for latest ordering. "
        "For qualitative or market questions, use database results as local evidence and web search as external context."
    )
    return "\n".join(lines)


def extract_text_and_sources_from_openai_response(data: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    output_text = data.get("output_text")
    chunks: list[str] = []
    sources: list[dict[str, str]] = []

    if isinstance(output_text, str) and output_text.strip():
        chunks.append(output_text.strip())

    for source in data.get("sources", []) or []:
        if isinstance(source, dict):
            url = str(source.get("url") or source.get("uri") or "").strip()
            title = str(source.get("title") or source.get("name") or url).strip()
            if url:
                sources.append({"title": title, "url": url})

    for output_item in data.get("output", []) or []:
        if not isinstance(output_item, dict):
            continue
        for content_item in output_item.get("content", []) or []:
            if not isinstance(content_item, dict):
                continue
            text = content_item.get("text")
            if isinstance(text, str) and text.strip() and not chunks:
                chunks.append(text.strip())
            for annotation in content_item.get("annotations", []) or []:
                if not isinstance(annotation, dict):
                    continue
                url = str(annotation.get("url") or "").strip()
                title = str(annotation.get("title") or url).strip()
                if url:
                    sources.append({"title": title, "url": url})

    deduped_sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for source in sources:
        if source["url"] in seen_urls:
            continue
        seen_urls.add(source["url"])
        deduped_sources.append(source)

    text = "\n".join(chunks).strip() if chunks else json.dumps(data, ensure_ascii=False, indent=2)
    return text, deduped_sources


def call_openai_responses_api(
    *,
    api_key: str,
    model: str,
    instructions: str,
    user_input: str,
    max_output_tokens: int = 1600,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": user_input,
        "max_output_tokens": max_output_tokens,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice

    response = requests.post(
        OPENAI_RESPONSES_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=(20, 180),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI API error {response.status_code}: {response.text[:1600]}")

    data = response.json()
    text, sources = extract_text_and_sources_from_openai_response(data)
    return text, sources, data


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def sql_without_comments(sql: str) -> str:
    sql = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql.strip()


def is_safe_read_only_sql(sql: str) -> tuple[bool, str]:
    cleaned = sql_without_comments(sql)
    if not cleaned:
        return False, "SQL is empty."

    without_trailing_semicolon = cleaned.rstrip().rstrip(";").strip()
    if ";" in without_trailing_semicolon:
        return False, "Only one SQL statement is allowed."

    lowered = without_trailing_semicolon.casefold()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False, "Only SELECT / WITH read-only queries are allowed."

    for word in AI_CHAT_DISALLOWED_SQL_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return False, f"Blocked unsafe SQL keyword/function: {word}."

    return True, without_trailing_semicolon


def add_limit_if_needed(sql: str, limit: int = AI_CHAT_MAX_ROWS) -> str:
    cleaned = sql.rstrip().rstrip(";").strip()
    lowered = cleaned.casefold()
    if re.search(r"\blimit\s+\d+\b", lowered):
        return cleaned
    return f"{cleaned}\nLIMIT {limit}"


def question_contains_any(question: str, words: list[str]) -> bool:
    q = question.casefold()
    return any(word.casefold() in q for word in words)


def heuristic_ai_route(question: str, user_mode: str) -> dict[str, Any]:
    if user_mode == "Database only":
        return {"needs_database": True, "needs_web": False, "db_question": question, "web_brief": "", "reason": "User selected Database only."}
    if user_mode == "Internet only":
        return {"needs_database": False, "needs_web": True, "db_question": "", "web_brief": question, "reason": "User selected Internet only."}
    if user_mode == "Database + Internet":
        return {"needs_database": True, "needs_web": True, "db_question": question, "web_brief": question, "reason": "User selected Database + Internet."}

    database_words = [
        "rera", "launch", "launches", "project", "projects", "promoter", "district", "tehsil", "village",
        "micro-market", "micro market", "approval", "approved", "inventory", "flat", "plot", "unit", "group housing",
    ]
    web_words = [
        "why", "reason", "reasons", "external", "market", "demand", "news", "infra", "infrastructure",
        "trend", "trends", "pricing", "overheated", "opportunity", "explain", "could explain",
    ]
    needs_database = question_contains_any(question, database_words)
    needs_web = question_contains_any(question, web_words)
    if not needs_database and not needs_web:
        needs_database = True
        needs_web = True
    return {
        "needs_database": needs_database,
        "needs_web": needs_web,
        "db_question": question if needs_database else "",
        "web_brief": question if needs_web else "",
        "reason": "Heuristic route used.",
    }


def deterministic_sql_for_question(question: str) -> dict[str, Any]:
    """Reliable fallback SQL when the model returns blank/invalid SQL."""
    q = question.casefold()
    jaipur_filter = "district_name ILIKE '%jaipur%'" if "jaipur" in q else "TRUE"

    if "micro" in q or "market" in q or "area" in q or "launch" in q:
        sql = f"""
WITH base AS (
    SELECT
        COALESCE(NULLIF(TRIM(village_name), ''), NULLIF(TRIM(tahsil_name), ''), NULLIF(TRIM(district_name), ''), 'Unknown') AS micro_market,
        MAX(NULLIF(TRIM(tahsil_name), '')) AS tehsil_name,
        MAX(NULLIF(TRIM(district_name), '')) AS district_name,
        COUNT(*) AS total_rera_projects,
        COUNT(*) FILTER (WHERE approved_year IS NOT NULL) AS projects_with_approval_year,
        COUNT(*) FILTER (WHERE approved_year >= EXTRACT(YEAR FROM CURRENT_DATE)::int - 2) AS recent_3yr_launches,
        COUNT(*) FILTER (WHERE raw_json IS NOT NULL) AS projects_with_detail_json,
        COUNT(DISTINCT NULLIF(TRIM(promoter_name), '')) AS promoter_count,
        COUNT(*) FILTER (WHERE project_type ILIKE '%group%' OR project_type ILIKE '%residential%') AS group_or_residential_projects,
        COUNT(*) FILTER (WHERE project_type ILIKE '%plot%') AS plotted_projects,
        COUNT(*) FILTER (WHERE project_type ILIKE '%commercial%') AS commercial_projects,
        MIN(approved_year) AS first_approval_year,
        MAX(approved_year) AS latest_approval_year,
        SUM(COALESCE(area_sqm, 0)) AS total_project_area_sqm,
        SUM(COALESCE(saleable_area_sqm, 0)) AS total_saleable_area_sqm
    FROM rera_projects
    WHERE {jaipur_filter}
    GROUP BY 1
)
SELECT
    micro_market,
    tehsil_name,
    district_name,
    total_rera_projects,
    recent_3yr_launches,
    promoter_count,
    group_or_residential_projects,
    plotted_projects,
    commercial_projects,
    first_approval_year,
    latest_approval_year,
    ROUND(total_project_area_sqm::numeric, 2) AS total_project_area_sqm,
    ROUND(total_saleable_area_sqm::numeric, 2) AS total_saleable_area_sqm
FROM base
WHERE micro_market <> 'Unknown'
ORDER BY recent_3yr_launches DESC, total_rera_projects DESC, promoter_count DESC, total_project_area_sqm DESC
LIMIT 30
""".strip()
        return {
            "sql": sql,
            "explanation": "Fallback query: ranks micro-markets by RERA project count, recent approvals, promoter activity, project type mix, and area size.",
            "confidence": "medium",
            "fallback_used": True,
        }

    if "promoter" in q or "developer" in q:
        sql = f"""
SELECT
    promoter_name,
    COUNT(*) AS total_projects,
    COUNT(*) FILTER (WHERE approved_year >= EXTRACT(YEAR FROM CURRENT_DATE)::int - 2) AS recent_3yr_projects,
    COUNT(DISTINCT COALESCE(NULLIF(TRIM(village_name), ''), NULLIF(TRIM(tahsil_name), ''), district_name)) AS micro_market_count,
    MIN(approved_year) AS first_approval_year,
    MAX(approved_year) AS latest_approval_year,
    SUM(COALESCE(area_sqm, 0)) AS total_project_area_sqm
FROM rera_projects
WHERE {jaipur_filter}
  AND promoter_name IS NOT NULL
  AND TRIM(promoter_name) <> ''
GROUP BY promoter_name
ORDER BY total_projects DESC, recent_3yr_projects DESC, micro_market_count DESC
LIMIT 30
""".strip()
        return {
            "sql": sql,
            "explanation": "Fallback query: ranks promoters/developers by RERA project count, recent activity, and spread across micro-markets.",
            "confidence": "medium",
            "fallback_used": True,
        }

    sql = f"""
SELECT
    district_name,
    tahsil_name,
    COALESCE(NULLIF(TRIM(village_name), ''), 'Unknown') AS area_or_village,
    project_type,
    approved_year,
    COUNT(*) AS project_count,
    COUNT(*) FILTER (WHERE raw_json IS NOT NULL) AS with_detail_json,
    SUM(COALESCE(area_sqm, 0)) AS total_project_area_sqm,
    SUM(COALESCE(saleable_area_sqm, 0)) AS total_saleable_area_sqm
FROM rera_projects
WHERE {jaipur_filter}
GROUP BY district_name, tahsil_name, COALESCE(NULLIF(TRIM(village_name), ''), 'Unknown'), project_type, approved_year
ORDER BY approved_year DESC NULLS LAST, project_count DESC
LIMIT 50
""".strip()
    return {
        "sql": sql,
        "explanation": "Fallback query: gives a project count breakdown by district, tehsil, area/village, project type, and approval year.",
        "confidence": "low",
        "fallback_used": True,
    }


def build_ai_history_context(history: list[dict[str, Any]] | None, max_turns: int = 4) -> str:
    if not history:
        return "No previous AI research conversation in this session."
    lines: list[str] = []
    for item in list(history)[:max_turns]:
        rows = item.get("rows") or []
        preview = rows[:5] if isinstance(rows, list) else []
        lines.append(
            "Previous turn:\n"
            f"Question: {item.get('question') or ''}\n"
            f"SQL: {item.get('sql') or ''}\n"
            f"DB preview rows: {json.dumps(preview, ensure_ascii=False)[:2000]}\n"
            f"Answer: {(item.get('answer') or '')[:2000]}"
        )
    return "\n\n".join(lines)


def plan_ai_research_question(question: str, user_mode: str, schema_summary: str, history_context: str = "") -> dict[str, Any]:
    if user_mode in {"Database only", "Internet only", "Database + Internet"}:
        return heuristic_ai_route(question, user_mode)

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in local env or Streamlit secrets.")

    instructions = """
You route questions for a Rajasthan RERA real-estate intelligence dashboard.
Return ONLY valid JSON:
{
  "needs_database": true/false,
  "needs_web": true/false,
  "db_question": "specific database question to answer with SQL, or empty",
  "web_brief": "specific internet research brief, or empty",
  "reason": "short routing reason"
}
Routing rules:
- Use database when the question asks about local RERA projects, promoters, districts, years, project types, statuses, areas, changes, documents, inventory, or comparisons inside the database.
- Use web when the question asks for market context, latest news, external pricing, competitor/developer reputation, regulations, infrastructure, demand drivers, qualitative opportunity analysis, or anything likely to be outside the local database.
- Use both when database evidence plus external interpretation would improve the answer.
- Follow-up wording like "those", "same", "among these", "compare them", or "why" should use previous conversation context.
- If unsure, use both.
""".strip()

    user_input = f"""
Database schema summary:
{schema_summary}

Previous conversation context:
{history_context}

User question:
{question}
""".strip()
    try:
        text, _sources, _raw = call_openai_responses_api(
            api_key=settings.openai_api_key,
            model=get_ai_research_model(),
            instructions=instructions,
            user_input=user_input,
            max_output_tokens=700,
        )
        plan = extract_json_object(text)
        return {
            "needs_database": bool(plan.get("needs_database")),
            "needs_web": bool(plan.get("needs_web")),
            "db_question": str(plan.get("db_question") or question).strip(),
            "web_brief": str(plan.get("web_brief") or question).strip(),
            "reason": str(plan.get("reason") or "Auto-routed question.").strip(),
        }
    except Exception as exc:  # noqa: BLE001
        route = heuristic_ai_route(question, user_mode)
        route["reason"] = f"Heuristic route used because AI routing failed: {exc}"
        return route


def generate_sql_for_ai_research(question: str, schema_summary: str, history_context: str = "") -> dict[str, Any]:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in local env or Streamlit secrets.")

    instructions = """
You are a careful PostgreSQL analyst for a local Rajasthan RERA real-estate intelligence database.
Return ONLY valid JSON with these keys:
{
  "sql": "...",
  "explanation": "short explanation of what the query does",
  "confidence": "high|medium|low"
}
Rules:
- You MUST return a non-empty SQL string.
- Generate exactly one read-only PostgreSQL query.
- Query only the tables and columns in the schema.
- Use SELECT or WITH only. Never use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, COPY, CALL, DO, EXECUTE, or unsafe functions.
- Prefer rera_projects for project-level questions.
- For Jaipur use: district_name ILIKE '%jaipur%'.
- For micro-market analysis, group by COALESCE(NULLIF(TRIM(village_name), ''), NULLIF(TRIM(tahsil_name), ''), district_name).
- For RERA launches, use approved_year and project counts.
- For recent launches, use approved_year >= EXTRACT(YEAR FROM CURRENT_DATE)::int - 2.
- Use aggregates for counts, district/year/type breakdowns, top promoters, statuses, area summaries, etc.
- Add a LIMIT of 200 for non-aggregate result lists.
- Do not select raw_json unless the user clearly asks about nested/raw details.
- For latest use COALESCE(last_changed_at, last_scraped_at, created_at) DESC when those columns exist.
- If the question is qualitative, produce a compact evidence query that gives useful local facts for the final qualitative answer.
Example for active Jaipur micro-markets: rank village_name/tehsil_name by total projects, recent approvals, promoter_count, project_type mix, area_sqm, saleable_area_sqm.
""".strip()

    user_input = f"""
Database schema:
{schema_summary}

Previous conversation context:
{history_context}

Database question:
{question}
""".strip()

    text = ""
    plan: dict[str, Any] = {}
    try:
        text, _sources, _raw = call_openai_responses_api(
            api_key=settings.openai_api_key,
            model=get_ai_research_model(),
            instructions=instructions,
            user_input=user_input,
            max_output_tokens=1100,
        )
        plan = extract_json_object(text)
    except Exception as exc:  # noqa: BLE001
        plan = {
            "sql": "",
            "explanation": f"AI SQL generation failed, deterministic fallback used. Error: {exc}",
            "confidence": "low",
        }

    sql = str(plan.get("sql") or "").strip()
    is_safe, safe_or_reason = is_safe_read_only_sql(sql)
    if not sql or not is_safe:
        fallback = deterministic_sql_for_question(question)
        fallback_sql = str(fallback.get("sql") or "").strip()
        fallback_safe, fallback_safe_or_reason = is_safe_read_only_sql(fallback_sql)
        if not fallback_safe:
            raise RuntimeError(
                "Generated SQL was blocked and fallback SQL also failed safety check: "
                f"{fallback_safe_or_reason}\n\nGenerated SQL:\n{sql}\n\nRaw AI text:\n{text[:1600]}"
            )
        fallback["sql"] = fallback_safe_or_reason
        if sql and not is_safe:
            fallback["explanation"] = f"AI SQL was unsafe/invalid ({safe_or_reason}), so fallback query was used. " + str(fallback.get("explanation") or "")
        elif not sql:
            fallback["explanation"] = "AI returned empty SQL, so fallback query was used. " + str(fallback.get("explanation") or "")
        return fallback

    plan["sql"] = add_limit_if_needed(safe_or_reason)
    plan["fallback_used"] = False
    return plan


def execute_ai_research_sql(sql: str) -> tuple[list[dict[str, Any]], list[str]]:
    is_safe, safe_or_reason = is_safe_read_only_sql(sql)
    if not is_safe:
        raise RuntimeError(safe_or_reason)
    sql = add_limit_if_needed(safe_or_reason)

    settings = get_settings()
    with get_connection(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL statement_timeout = '{AI_CHAT_STATEMENT_TIMEOUT_MS}ms'")
            cursor.execute(sql)
            rows = cursor.fetchall()
            column_names = [desc.name for desc in cursor.description] if cursor.description else []

    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            normalized_rows.append({key: json_safe(value) for key, value in row.items()})
        else:
            normalized_rows.append({column_names[index]: json_safe(value) for index, value in enumerate(row)})
    return normalized_rows, column_names


def answer_ai_research_question(
    *,
    original_question: str,
    route: dict[str, Any],
    sql: str | None,
    sql_explanation: str | None,
    rows: list[dict[str, Any]],
    columns: list[str],
    history_context: str = "",
) -> tuple[str, list[dict[str, str]], bool]:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in local env or Streamlit secrets.")

    needs_web = bool(route.get("needs_web"))
    tools = None
    tool_choice = None
    if needs_web:
        tools = [{"type": "web_search", "search_context_size": "low"}]
        tool_choice = "auto"

    instructions = """
You are a real-estate research analyst for a Rajasthan RERA intelligence dashboard.
You may receive local database evidence and, when web_search is enabled, you should use live internet research for external context.
Answer qualitative questions directly, with practical business interpretation.
Rules:
- Clearly separate: local database evidence, external market/web context, and conclusion/action.
- For micro-market questions, treat village_name/tehsil_name groupings as imperfect proxies, not official market boundaries.
- Do not pretend web research was done if no web tool was available or used.
- Use cautious language where data is incomplete.
- For opportunity analysis, mention target segment, demand trigger, validation test, acquisition/channel hypothesis, risks/signals/mitigations.
- Keep the answer concise but decision-useful.
- Do not invent database facts beyond supplied rows.
""".strip()

    user_payload = {
        "original_question": original_question,
        "previous_conversation_context": history_context,
        "routing": route,
        "database_sql": sql,
        "database_sql_explanation": sql_explanation,
        "database_columns": columns,
        "database_row_count_returned": len(rows),
        "database_rows_preview": rows[:100],
        "instruction": "Answer the user using the database evidence and, if enabled, web search.",
    }

    text, sources, _raw = call_openai_responses_api(
        api_key=settings.openai_api_key,
        model=get_ai_research_model(),
        instructions=instructions,
        user_input=json.dumps(user_payload, ensure_ascii=False, indent=2),
        max_output_tokens=2400,
        tools=tools,
        tool_choice=tool_choice,
    )
    return text, sources, needs_web


def run_ai_research_question(question: str, user_mode: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    schema_summary = load_ai_database_schema_summary()
    history_context = build_ai_history_context(history)
    route = plan_ai_research_question(question, user_mode, schema_summary, history_context)

    sql: str | None = None
    sql_explanation: str | None = None
    sql_confidence: str | None = None
    rows: list[dict[str, Any]] = []
    columns: list[str] = []
    fallback_used = False

    if route.get("needs_database"):
        sql_plan = generate_sql_for_ai_research(route.get("db_question") or question, schema_summary, history_context)
        sql = sql_plan["sql"]
        sql_explanation = str(sql_plan.get("explanation") or "")
        sql_confidence = str(sql_plan.get("confidence") or "")
        fallback_used = bool(sql_plan.get("fallback_used"))
        rows, columns = execute_ai_research_sql(sql)

    answer, sources, web_attempted = answer_ai_research_question(
        original_question=question,
        route=route,
        sql=sql,
        sql_explanation=sql_explanation,
        rows=rows,
        columns=columns,
        history_context=history_context,
    )

    return {
        "question": question,
        "mode": user_mode,
        "route": route,
        "answer": answer,
        "sources": sources,
        "web_attempted": web_attempted,
        "sql": sql,
        "sql_explanation": sql_explanation,
        "sql_confidence": sql_confidence,
        "sql_fallback_used": fallback_used,
        "rows": rows,
        "columns": columns,
    }


def render_ai_research_panel() -> None:
    st.markdown("### Ask AI: database + internet research")
    st.caption(
        "Ask about schema, projects, market data, ROI, changes, or external real-estate context. "
        "Examples: 'What columns are in project_roi_cases?', 'Which Jaipur micro-markets are most active?', "
        "'Summarize this project's booking status', or 'What infra news could affect these launches?'"
    )
    try:
        settings = get_settings()
    except RuntimeError as exc:
        st.info(f"AI research panel is unavailable until configuration is complete: {exc}")
        return

    if not settings.openai_api_key:
        st.warning("OPENAI_API_KEY was not found in local env or Streamlit secrets.")
        return

    if "ai_research_history" not in st.session_state:
        st.session_state.ai_research_history = []

    col1, col2 = st.columns([3, 1])
    with col1:
        question = st.text_area(
            "Question",
            key="ai_research_question",
            placeholder="Example: Which Jaipur micro-markets look most active based on RERA launches, and what external market reasons could explain it?",
            height=100,
        )
    with col2:
        mode = st.selectbox(
            "Research mode",
            ["Auto", "Database only", "Internet only", "Database + Internet"],
            index=0,
        )
        st.text_input("OpenAI model", value=get_ai_chat_model(), disabled=True)
        show_schema = st.checkbox("Show DB schema", value=False)

    ask_clicked = st.button("Ask AI research agent", type="primary", use_container_width=True)
    clear_clicked = st.button("Clear AI research history", use_container_width=True)

    if clear_clicked:
        st.session_state.ai_research_history = []
        st.rerun()

    if show_schema:
        with st.expander("Database schema summary", expanded=False):
            st.code(get_ai_chat_schema_summary(), language="text")

    if ask_clicked:
        if not clean_text(question):
            st.warning("Type a question first.")
        else:
            with st.spinner("Thinking, using database tools, searching the web if needed, and preparing the answer..."):
                result = safe_ask_ai_chat(question.strip(), mode, st.session_state.ai_research_history)
            st.session_state.ai_research_history.insert(0, result)

    for index, item in enumerate(st.session_state.ai_research_history[:10]):
        with st.container(border=True):
            st.markdown(f"**You asked:** {item['question']}")
            st.caption(
                f"Mode: {item.get('mode')} | "
                f"Model: {item.get('model') or 'NA'} | "
                f"Database used: {'yes' if item.get('used_database') else 'no'} | "
                f"Internet used: {'yes' if item.get('used_web') else 'no'}"
            )
            if item.get("error"):
                st.error(f"AI chat failed: {item['error']}")
                continue

            st.markdown(item.get("answer") or "")

            sources = item.get("sources") or []
            if sources:
                with st.expander("Web sources used", expanded=False):
                    for source in sources[:20]:
                        st.markdown(f"- [{source.get('title') or source.get('url')}]({source.get('url')})")
            elif item.get("used_web"):
                st.caption("The model used web search, but the response did not return explicit source URLs.")

            sql_queries = item.get("sql_queries") or []
            if sql_queries:
                with st.expander("Database SQL used", expanded=False):
                    for sql_index, sql_item in enumerate(sql_queries, start=1):
                        st.caption(f"{sql_index}. Tool: {sql_item.get('tool') or 'database'}")
                        st.code(sql_item.get("sql") or "", language="sql")

            tool_events = item.get("tool_events") or []
            if tool_events:
                with st.expander("Tool trace", expanded=False):
                    for event in tool_events:
                        line = f"- `{event.get('tool')}`"
                        if event.get("ok"):
                            st.markdown(line)
                        else:
                            st.markdown(f"{line} failed: {event.get('error')}")

            rows = item.get("data_preview_rows") or []
            if rows:
                with st.expander(f"Data preview: {item.get('data_preview_label') or 'rows'}", expanded=False):
                    df = pd.DataFrame(rows)
                    st.dataframe(df, use_container_width=True, hide_index=True)
                    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "Download preview CSV",
                        data=csv_bytes,
                        file_name=f"ai_research_result_{index + 1}.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )

def main() -> None:
    inject_styles()
    st.markdown(
        """
        <div class="hero">
            <h1>RERA Rajasthan Intelligence MVP</h1>
            <p>Search the local Rajasthan RERA dataset, inspect the latest raw JSON, and review field-level change history.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        get_settings()
    except RuntimeError as exc:
        render_missing_configuration(str(exc))
        return

    db_ready, db_message = get_database_bootstrap_status()
    if not db_ready:
        render_database_setup_required(db_message or "Database is not ready.")
        return

    with st.expander("Ask AI Research Agent (database + internet)", expanded=False):
        render_ai_research_panel()

    filter_options = load_filter_options()
    with st.sidebar:
        st.header("Filters")
        search = st.text_input("Project search", placeholder="Name, registration, promoter, encrypted ID")
        districts = st.multiselect(
            "District",
            filter_options["districts"],
            default=default_district_selection(filter_options["districts"]),
        )
        promoter_search = st.text_input("Promoter filter", placeholder="Contains promoter name")
        use_professional_filter = st.checkbox(
            "Enable professional filter",
            value=False,
            help="Loads professional names on demand because this filter is heavier than the others.",
        )
        selected_professionals: list[str] = []
        project_indexes: dict[str, Any] | None = None
        if use_professional_filter:
            project_indexes = load_project_indexes()
            selected_professionals = st.multiselect("Professional", project_indexes["professional_names"])
        project_types = st.multiselect("Project type", filter_options["project_types"])
        approved_years = st.multiselect("Approved year", filter_options["approved_years"])
        only_with_raw_json = st.checkbox("Only projects with raw JSON")
        changed_recently = st.checkbox("Changed recently")
        changed_days = st.selectbox("Changed within days", [7, 30, 90, 180], index=1)

    projects = query_projects(
        search=search,
        districts=districts,
        promoter_search=promoter_search,
        project_types=project_types,
        approved_years=approved_years,
        changed_recently=changed_recently,
        changed_days=changed_days,
        only_with_raw_json=only_with_raw_json,
    )

    using_professional_filter = bool(selected_professionals and project_indexes)
    if using_professional_filter:
        selected_professional_keys = {
            text_key(name) for name in selected_professionals if text_key(name)
        }
        projects = [
            project
            for project in projects
            if selected_professional_keys
            & project_indexes["by_project"]
            .get(project["encrypted_project_id"], {})
            .get("professional_name_keys", set())
        ]
        with st.sidebar:
            st.caption("Professional filtering scans local project JSONs, so it is slower than the default list.")

    stats = {
        "filtered_projects": len(projects),
        "with_raw_json": sum(1 for row in projects if row.get("has_raw_json")),
        "with_tracked_changes": sum(1 for row in projects if row.get("last_changed_at")),
    }

    auto_load_inventory = len(projects) <= 300
    with st.sidebar:
        inventory_mode = st.segmented_control(
            "Inventory columns",
            options=["Auto", "Show", "Hide"],
            default="Auto",
            help="Auto loads inventory for smaller result sets. Show forces it on, and Hide keeps the list faster.",
        )
        load_inventory_in_list = (
            auto_load_inventory if inventory_mode == "Auto" else inventory_mode == "Show"
        )

    metric1, metric2, metric3 = st.columns(3)
    metric1.metric("Filtered projects", int(stats.get("filtered_projects") or 0))
    metric2.metric("With raw JSON", int(stats.get("with_raw_json") or 0))
    metric3.metric("With tracked changes", int(stats.get("with_tracked_changes") or 0))

    if projects:
        inventory_index: dict[str, dict[str, Any]] = {}
        if load_inventory_in_list:
            inventory_index = load_inventory_for_projects(
                tuple(row["encrypted_project_id"] for row in projects if row.get("encrypted_project_id"))
            )
        display_rows = []
        for row in projects:
            inventory = inventory_index.get(
                row["encrypted_project_id"],
                {"label": "NA", "total": None, "sold": None, "unsold": None},
            )
            display_rows.append(
                {
                    "Project": row.get("project_name"),
                    "Registration": row.get("registration_no"),
                    "District": row.get("district_name"),
                    "Street Name": row.get("street_name"),
                    "Area / Village": row.get("village_name"),
                    "Promoter": row.get("promoter_name"),
                    "Type": row.get("project_type"),
                    "Approved Year": row.get("approved_year"),
                    "Status": row.get("project_status"),
                    "Sold": int_display(inventory.get("sold")),
                    "Total Units / Plots / Flats": int_display(inventory.get("total")),
                    "Unsold": int_display(inventory.get("unsold")),
                    "Project Area": area_display(row.get("area_sqm")),
                    "Phase Area": area_display(row.get("phase_area_sqm")),
                    "BUA / Saleable Area": area_display(row.get("saleable_area_sqm")),
                    "Raw JSON": row.get("has_raw_json"),
                    "Last Changed": row.get("last_changed_at"),
                }
            )

        st.caption(
            "Select a row to load project details on the same page, "
            "or use the project selector below. Inventory is conservative; unclear sold data is shown as NA instead of guessed."
        )
        if not load_inventory_in_list and len(projects) > 300:
            st.caption("Inventory columns are hidden for this large result set right now. Change the sidebar control to Show if you want them populated.")
        display_df = pd.DataFrame(display_rows)
        selected_rows = []
        try:
            table_event = st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
            )
            selected_rows = getattr(getattr(table_event, "selection", None), "rows", []) or []
        except TypeError:
            # Older Streamlit versions do not support dataframe row selection.
            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
            )
            st.caption("Your Streamlit version does not support row-click selection. Use the project selector below.")

        if selected_rows:
            selected_index = int(selected_rows[0])
            if 0 <= selected_index < len(projects):
                clicked_project_id = projects[selected_index]["encrypted_project_id"]
                current_param = st.query_params.get("project", "")
                if isinstance(current_param, list):
                    current_param = current_param[0] if current_param else ""
                if current_param != clicked_project_id:
                    st.query_params["project"] = clicked_project_id
                    st.rerun()
    else:
        st.info("No projects matched the current filters.")

    projects_by_id = {
        row["encrypted_project_id"]: row
        for row in projects
    }
    query_params = st.query_params
    current_project_id = query_params.get("project", "")
    if isinstance(current_project_id, list):
        current_project_id = current_project_id[0] if current_project_id else ""
    if current_project_id and current_project_id not in projects_by_id:
        current_project = load_project(current_project_id)
        if current_project is not None:
            projects_by_id[current_project_id] = current_project
        else:
            current_project_id = ""

    selectable_ids = [""] + list(projects_by_id.keys())
    selected_project_id = st.selectbox(
        "Project detail page / fallback selector",
        options=selectable_ids,
        index=selectable_ids.index(current_project_id) if current_project_id in selectable_ids else 0,
        format_func=lambda encrypted_id: (
            "Choose a filtered project"
            if not encrypted_id
            else format_project_option(encrypted_id, projects_by_id)
        ),
    )

    if not selected_project_id:
        query_params.clear()
        return

    query_params["project"] = selected_project_id
    project = load_project(selected_project_id)
    if project is None:
        st.warning("Project not found.")
        return

    st.subheader(project.get("project_name") or selected_project_id)
    overview_tab, market_tab, professionals_tab, structured_tab, documents_tab, raw_json_tab, change_log_tab = st.tabs(
        ["Overview & Map", "Market Pricing", "Professionals", "Structured JSON", "Documents", "Raw JSON", "Change Log"]
    )

    with overview_tab:
        render_project_overview(project)
    with market_tab:
        render_market_pricing_tab(project)
    with professionals_tab:
        render_professionals_tab(project)
    with structured_tab:
        render_structured_highlights(project)
    with documents_tab:
        render_documents_tab(project)
    with raw_json_tab:
        render_raw_json_explorer(project)
    with change_log_tab:
        changes = load_project_changes(selected_project_id)
        if changes:
            st.dataframe(pd.DataFrame(changes), use_container_width=True, hide_index=True)
        else:
            st.info("No tracked changes recorded for this project yet.")


if __name__ == "__main__":
    main()
