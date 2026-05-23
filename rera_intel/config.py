from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
OPENAI_KEY_FILE = ROOT_DIR / "openaikey"
DEFAULT_CSV_PATH = ROOT_DIR / "rajasthan_rera_projects.csv"
DEFAULT_JSON_DIR = ROOT_DIR / "rera_project_detail_jsons"
DEFAULT_LIST_API_URL = "https://reraapi.rajasthan.gov.in/api/web/Home/GetProjects"
DEFAULT_AUTO_SYNC_STATE_PATH = ROOT_DIR / ".rera_auto_sync_state.json"
DEFAULT_AUTO_SYNC_LOCK_PATH = ROOT_DIR / ".rera_auto_sync.lock"
DEFAULT_DETAIL_SYNC_FAILURE_STATE_PATH = ROOT_DIR / ".rera_detail_sync_failures.json"

load_dotenv(ENV_PATH)

try:
    import streamlit as st
except Exception:  # pragma: no cover - streamlit may be unavailable in non-UI contexts
    st = None


@dataclass(frozen=True)
class Settings:
    database_url: str
    rera_api_key: str | None
    serpapi_key: str | None
    serpapi_gl: str
    serpapi_hl: str
    serpapi_location: str
    detail_sync_max_projects_per_run: int
    detail_sync_refresh_days: int
    detail_sync_candidate_scan_multiplier: int
    detail_sync_failure_cooldown_hours: int
    detail_sync_failure_state_path: Path
    auto_sync_afternoon_hour: int
    auto_sync_afternoon_minute: int
    auto_sync_interval_minutes: int
    auto_sync_state_path: Path
    auto_sync_lock_path: Path
    geocoding_endpoint: str
    geocoding_reverse_endpoint: str
    geocoding_user_agent: str
    geocoding_email: str | None
    whatsapp_verify_token: str | None
    whatsapp_access_token: str | None
    whatsapp_phone_number_id: str | None
    whatsapp_allowed_numbers: tuple[str, ...]
    whatsapp_graph_api_version: str
    whatsapp_reply_max_chars: int
    openai_api_key: str | None
    openai_summary_model: str
    csv_path: Path
    json_dir: Path
    list_api_url: str


def _read_streamlit_secret_path(path: str) -> str | None:
    if st is None:
        return None
    try:
        value: object = st.secrets
        for part in path.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = value[part]
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_setting(key: str, default: str = "", aliases: tuple[str, ...] = ()) -> str:
    candidates = (key, *aliases)
    for candidate in candidates:
        env_value = os.getenv(candidate, "").strip()
        if env_value:
            return env_value

    secret_paths: list[str] = []
    for candidate in candidates:
        secret_paths.extend(
            [
                candidate,
                candidate.lower(),
                candidate.upper(),
                f"default.{candidate}",
                f"default.{candidate.lower()}",
                f"secrets.{candidate}",
                f"secrets.{candidate.lower()}",
            ]
        )
    if key == "DATABASE_URL":
        secret_paths.extend(
            [
                "database.url",
                "database.database_url",
                "postgres.url",
                "postgres.database_url",
                "postgresql.url",
                "postgresql.database_url",
                "connections.postgresql.url",
                "connections.postgres.url",
            ]
        )

    for path in secret_paths:
        secret_value = _read_streamlit_secret_path(path)
        if secret_value:
            return secret_value
    return default


def _parse_csv_setting(value: str) -> tuple[str, ...]:
    items = []
    for part in (value or "").split(","):
        cleaned = part.strip()
        if cleaned:
            items.append(cleaned)
    return tuple(items)


def get_settings(*, require_api_key: bool = False) -> Settings:
    # Re-read .env on each access so Streamlit picks up newly added keys
    # without requiring a full process restart.
    load_dotenv(ENV_PATH)

    database_url = _read_setting("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is missing. Add it to .env locally or Streamlit secrets in the cloud."
        )

    rera_api_key = _read_setting("RERA_API_KEY") or None
    if require_api_key and not rera_api_key:
        raise RuntimeError(
            "RERA_API_KEY is missing. Add it to .env locally or Streamlit secrets in the cloud."
        )

    serpapi_key = _read_setting("SERPAPI_KEY") or None
    serpapi_gl = _read_setting("SERPAPI_GL", "in") or "in"
    serpapi_hl = _read_setting("SERPAPI_HL", "en") or "en"
    serpapi_location = _read_setting("SERPAPI_LOCATION", "Rajasthan, India") or "Rajasthan, India"
    detail_sync_max_projects_per_run = int(
        _read_setting("DETAIL_SYNC_MAX_PROJECTS_PER_RUN", "100") or "100"
    )
    detail_sync_refresh_days = int(
        _read_setting("DETAIL_SYNC_REFRESH_DAYS", "30") or "30"
    )
    detail_sync_candidate_scan_multiplier = int(
        _read_setting("DETAIL_SYNC_CANDIDATE_SCAN_MULTIPLIER", "3") or "3"
    )
    detail_sync_failure_cooldown_hours = int(
        _read_setting("DETAIL_SYNC_FAILURE_COOLDOWN_HOURS", "24") or "24"
    )
    detail_sync_failure_state_path = Path(
        _read_setting(
            "DETAIL_SYNC_FAILURE_STATE_PATH",
            str(DEFAULT_DETAIL_SYNC_FAILURE_STATE_PATH),
        )
    )
    auto_sync_afternoon_hour = int(
        _read_setting("AUTO_SYNC_AFTERNOON_HOUR", "14") or "14"
    )
    auto_sync_afternoon_minute = int(
        _read_setting("AUTO_SYNC_AFTERNOON_MINUTE", "0") or "0"
    )
    auto_sync_interval_minutes = int(
        _read_setting("AUTO_SYNC_INTERVAL_MINUTES", "30") or "30"
    )
    auto_sync_state_path = Path(
        _read_setting("AUTO_SYNC_STATE_PATH", str(DEFAULT_AUTO_SYNC_STATE_PATH))
    )
    auto_sync_lock_path = Path(
        _read_setting("AUTO_SYNC_LOCK_PATH", str(DEFAULT_AUTO_SYNC_LOCK_PATH))
    )
    geocoding_endpoint = _read_setting(
        "GEOCODING_ENDPOINT",
        "https://nominatim.openstreetmap.org/search",
    )
    geocoding_reverse_endpoint = _read_setting(
        "GEOCODING_REVERSE_ENDPOINT",
        "https://nominatim.openstreetmap.org/reverse",
    )
    geocoding_user_agent = _read_setting(
        "GEOCODING_USER_AGENT",
        "rera-rajasthan-intel/1.0",
    ) or "rera-rajasthan-intel/1.0"
    geocoding_email = _read_setting("GEOCODING_EMAIL") or None

    whatsapp_verify_token = _read_setting("WHATSAPP_VERIFY_TOKEN") or None
    whatsapp_access_token = _read_setting("WHATSAPP_ACCESS_TOKEN") or None
    whatsapp_phone_number_id = _read_setting("WHATSAPP_PHONE_NUMBER_ID") or None
    whatsapp_allowed_numbers = _parse_csv_setting(_read_setting("WHATSAPP_ALLOWED_NUMBERS"))
    whatsapp_graph_api_version = _read_setting("WHATSAPP_GRAPH_API_VERSION", "v23.0") or "v23.0"
    whatsapp_reply_max_chars = int(
        _read_setting("WHATSAPP_REPLY_MAX_CHARS", "1200") or "1200"
    )

    openai_api_key = _read_setting("OPENAI_API_KEY") or None
    if not openai_api_key and OPENAI_KEY_FILE.exists():
        openai_api_key = OPENAI_KEY_FILE.read_text(encoding="utf-8").strip() or None
    openai_summary_model = _read_setting("OPENAI_SUMMARY_MODEL", "gpt-5.5")

    csv_path = Path(_read_setting("RERA_CSV_PATH", str(DEFAULT_CSV_PATH)))
    json_dir = Path(_read_setting("RERA_JSON_DIR", str(DEFAULT_JSON_DIR)))
    list_api_url = _read_setting("RERA_LIST_API_URL", DEFAULT_LIST_API_URL)

    return Settings(
        database_url=database_url,
        rera_api_key=rera_api_key,
        serpapi_key=serpapi_key,
        serpapi_gl=serpapi_gl,
        serpapi_hl=serpapi_hl,
        serpapi_location=serpapi_location,
        detail_sync_max_projects_per_run=detail_sync_max_projects_per_run,
        detail_sync_refresh_days=detail_sync_refresh_days,
        detail_sync_candidate_scan_multiplier=detail_sync_candidate_scan_multiplier,
        detail_sync_failure_cooldown_hours=detail_sync_failure_cooldown_hours,
        detail_sync_failure_state_path=detail_sync_failure_state_path,
        auto_sync_afternoon_hour=auto_sync_afternoon_hour,
        auto_sync_afternoon_minute=auto_sync_afternoon_minute,
        auto_sync_interval_minutes=auto_sync_interval_minutes,
        auto_sync_state_path=auto_sync_state_path,
        auto_sync_lock_path=auto_sync_lock_path,
        geocoding_endpoint=geocoding_endpoint,
        geocoding_reverse_endpoint=geocoding_reverse_endpoint,
        geocoding_user_agent=geocoding_user_agent,
        geocoding_email=geocoding_email,
        whatsapp_verify_token=whatsapp_verify_token,
        whatsapp_access_token=whatsapp_access_token,
        whatsapp_phone_number_id=whatsapp_phone_number_id,
        whatsapp_allowed_numbers=whatsapp_allowed_numbers,
        whatsapp_graph_api_version=whatsapp_graph_api_version,
        whatsapp_reply_max_chars=whatsapp_reply_max_chars,
        openai_api_key=openai_api_key,
        openai_summary_model=openai_summary_model,
        csv_path=csv_path,
        json_dir=json_dir,
        list_api_url=list_api_url,
    )
