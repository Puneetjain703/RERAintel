from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import requests

from .config import Settings
from .db import get_connection
from .rera_sync import run_incremental_sync


def now_local() -> datetime:
    return datetime.now().astimezone()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_internet_available(timeout_seconds: int = 15) -> bool:
    try:
        response = requests.get(
            "https://rera.rajasthan.gov.in/",
            timeout=(5, timeout_seconds),
        )
        return response.status_code < 500
    except requests.RequestException:
        return False


def should_run_now(
    *,
    state: dict[str, Any],
    afternoon_hour: int,
    afternoon_minute: int,
    force: bool,
) -> tuple[bool, str]:
    if force:
        return True, "force"

    current_time = now_local()
    last_success = parse_datetime(state.get("last_success_at"))
    if last_success is None:
        return True, "first_run"

    afternoon_cutoff = datetime.combine(
        current_time.date(),
        time(hour=afternoon_hour, minute=afternoon_minute),
        tzinfo=current_time.tzinfo,
    )

    if current_time < afternoon_cutoff:
        return False, "before_afternoon_window"

    if last_success >= afternoon_cutoff:
        return False, "already_synced_this_afternoon"

    return True, "due_after_afternoon_window"


@contextmanager
def advisory_lock(lock_path: Path, *, stale_after_hours: int = 8):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        age = now_local() - datetime.fromtimestamp(lock_path.stat().st_mtime).astimezone()
        if age > timedelta(hours=stale_after_hours):
            lock_path.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"Another auto sync run appears active: {lock_path}")

    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
        yield
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def serialize_stats(value: Any) -> Any:
    if is_dataclass(value):
        return {key: serialize_stats(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: serialize_stats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize_stats(item) for item in value]
    return value


def run_auto_update(settings: Settings, *, force: bool = False) -> dict[str, Any]:
    state = load_state(settings.auto_sync_state_path)
    current_time = now_local()
    should_run, reason = should_run_now(
        state=state,
        afternoon_hour=settings.auto_sync_afternoon_hour,
        afternoon_minute=settings.auto_sync_afternoon_minute,
        force=force,
    )
    result: dict[str, Any] = {
        "timestamp": current_time.isoformat(),
        "should_run": should_run,
        "reason": reason,
        "internet_available": False,
        "ran_sync": False,
    }

    if not should_run:
        save_state(
            settings.auto_sync_state_path,
            {
                **state,
                "last_check_at": current_time.isoformat(),
                "last_decision": result,
            },
        )
        return result

    if not is_internet_available():
        result["reason"] = "offline"
        save_state(
            settings.auto_sync_state_path,
            {
                **state,
                "last_check_at": current_time.isoformat(),
                "last_attempt_at": current_time.isoformat(),
                "last_decision": result,
            },
        )
        return result

    result["internet_available"] = True

    with advisory_lock(settings.auto_sync_lock_path):
        with get_connection(settings.database_url) as connection:
            sync_stats = run_incremental_sync(
                connection,
                api_url=settings.list_api_url,
                api_key=settings.rera_api_key or "",
                csv_path=settings.csv_path,
                json_dir=settings.json_dir,
                max_detail_projects=settings.detail_sync_max_projects_per_run,
                refresh_days=settings.detail_sync_refresh_days,
                candidate_scan_multiplier=settings.detail_sync_candidate_scan_multiplier,
                failure_cooldown_hours=settings.detail_sync_failure_cooldown_hours,
                failure_state_path=settings.detail_sync_failure_state_path,
            )

        finished_at = now_local()
        result["ran_sync"] = True
        result["completed_at"] = finished_at.isoformat()
        result["sync_stats"] = serialize_stats(sync_stats)

        save_state(
            settings.auto_sync_state_path,
            {
                **state,
                "last_check_at": current_time.isoformat(),
                "last_attempt_at": current_time.isoformat(),
                "last_success_at": finished_at.isoformat(),
                "last_decision": result,
            },
        )

    return result
