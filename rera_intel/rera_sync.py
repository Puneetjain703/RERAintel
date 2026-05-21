from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .ingest import ingest_json_file, upsert_listing_row, utcnow


DEFAULT_LIST_PAYLOAD = {
    "DistrictId": "0",
    "TeshilId": "0",
    "ProjectName": None,
    "PromoterName": None,
    "RegistrationNo": None,
    "ProjectType": 0,
    "ApplicationStatus": "0",
    "Year": 0,
}

DETAIL_BRIDGE_URL = "https://reraapp.rajasthan.gov.in/HomeWebsite/ProjectDtlsWebsite/{encrypted_project_id}"
DETAIL_JSON_URL = "https://reraapp.rajasthan.gov.in/HomeWebsite/ViewProjectWebsite"


@dataclass
class DetailSyncStats:
    candidates_selected: int = 0
    candidates_suppressed_recent_failures: int = 0
    projects_attempted: int = 0
    projects_fetched: int = 0
    json_files_saved: int = 0
    projects_inserted: int = 0
    projects_seeded: int = 0
    projects_changed: int = 0
    projects_unchanged: int = 0
    snapshots_inserted: int = 0
    change_rows_inserted: int = 0
    projects_failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class SyncRunStats:
    csv_rows_fetched: int = 0
    listing_rows_processed: int = 0
    listing_rows_inserted: int = 0
    detail_sync: DetailSyncStats = field(default_factory=DetailSyncStats)


def build_rera_headers(api_key: str) -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://rera.rajasthan.gov.in",
        "referer": "https://rera.rajasthan.gov.in/",
        "user-agent": "Mozilla/5.0",
        "x-api-key": api_key,
    }


# def fetch_project_list(api_url: str, api_key: str) -> list[dict[str, Any]]:
#     response = requests.post(
#         api_url,
#         headers=build_rera_headers(api_key),
#         json=DEFAULT_LIST_PAYLOAD,
#         timeout=(20, 90),
#     )
#     response.raise_for_status()

#     payload = response.json()
#     if payload.get("Data") is None:
#         raise RuntimeError(f"Unexpected response from RERA API: {payload}")
#     return payload["Data"]
def fetch_project_list(api_url: str, api_key: str) -> list[dict[str, Any]]:
    """
    Fetch all Rajasthan RERA project listing rows.

    The API may return only a fixed number of rows per request.
    This function tries paginated payload keys and stops when no new rows arrive.
    """
    all_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    page_size = 1000
    page = 1

    while True:
        payload_to_send = {
            **DEFAULT_LIST_PAYLOAD,

            # Common pagination names. If the API ignores unknown keys,
            # this is harmless. If it supports any of them, pagination works.
            "Page": page,
            "PageNo": page,
            "PageNumber": page,
            "PageIndex": page,
            "page": page,
            "pageNo": page,
            "pageNumber": page,

            "PageSize": page_size,
            "pageSize": page_size,
            "Take": page_size,
            "take": page_size,
            "Length": page_size,
            "length": page_size,
        }

        response = requests.post(
            api_url,
            headers=build_rera_headers(api_key),
            json=payload_to_send,
            timeout=(20, 90),
        )
        response.raise_for_status()

        payload = response.json()
        rows = payload.get("Data")

        if rows is None:
            raise RuntimeError(f"Unexpected response from RERA API: {payload}")

        if not rows:
            break

        new_rows = 0
        for row in rows:
            encrypted_id = (row.get("EncryptedProjectId") or "").strip()

            # If no ID exists, still keep the row.
            if not encrypted_id:
                all_rows.append(row)
                new_rows += 1
                continue

            if encrypted_id in seen_ids:
                continue

            seen_ids.add(encrypted_id)
            all_rows.append(row)
            new_rows += 1

        print(
            f"Fetched page {page}: {len(rows)} rows, "
            f"{new_rows} new, total unique: {len(all_rows)}"
        )

        # Stop if API returned less than a full page.
        if len(rows) < page_size:
            break

        # Stop if pagination keys are ignored and the same first page repeats.
        if new_rows == 0:
            print("No new rows found. Pagination may not be supported by this endpoint.")
            break

        page += 1

    return all_rows

def write_csv(rows: list[dict[str, Any]], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def safe_filename(value: Any) -> str:
    text = re.sub(r'[\\/*?:"<>|]', "_", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text[:150]


def choose_file_part(*values: Any) -> str | None:
    for value in values:
        text = safe_filename(value)
        if text:
            return text
    return None


def build_json_output_path(
    *,
    json_dir: Path,
    encrypted_project_id: str,
    csv_row: dict[str, Any] | None,
    existing_source_file: str | None,
) -> Path:
    if existing_source_file:
        existing_path = Path(existing_source_file)
        if existing_path.suffix.lower() == ".json":
            return existing_path

    direct_matches = sorted(json_dir.glob(f"*{encrypted_project_id}.json"))
    if direct_matches:
        return direct_matches[0]

    csv_row = csv_row or {}
    registration_no = choose_file_part(
        csv_row.get("REGISTRATIONNO"),
        csv_row.get("RegistrationNo"),
    )
    project_name = choose_file_part(
        csv_row.get("ProjectName"),
        csv_row.get("project_name"),
    )
    encrypted_part = safe_filename(encrypted_project_id)
    parts = [part for part in [registration_no, project_name, encrypted_part] if part]
    file_name = "_".join(parts) if parts else encrypted_part
    return json_dir / f"{file_name}.json"


def fetch_project_detail_bridge(
    session: requests.Session,
    *,
    encrypted_project_id: str,
) -> dict[str, Any]:
    response = session.get(
        DETAIL_BRIDGE_URL.format(encrypted_project_id=encrypted_project_id),
        timeout=(20, 90),
    )
    response.raise_for_status()

    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Bridge endpoint failed for {encrypted_project_id}: {payload}")
    data = payload.get("data") or {}
    project_id = str(data.get("ProjectId") or "").strip()
    if not project_id:
        raise RuntimeError(f"Bridge endpoint returned no internal ProjectId for {encrypted_project_id}")
    return payload


def fetch_project_detail_json(
    session: requests.Session,
    *,
    encrypted_project_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    bridge_payload = fetch_project_detail_bridge(
        session,
        encrypted_project_id=encrypted_project_id,
    )
    project_id = bridge_payload["data"]["ProjectId"]
    response = session.get(
        DETAIL_JSON_URL,
        params={"id": project_id, "type": "U"},
        timeout=(20, 120),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or "GetProjectBasic" not in payload:
        raise RuntimeError(
            f"Unexpected full project JSON payload for {encrypted_project_id}: {type(payload).__name__}"
        )
    return payload, bridge_payload


def save_project_json(path: Path, raw_json: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(raw_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_failure_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): value
        for key, value in payload.items()
        if isinstance(value, dict)
    }


def save_failure_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_existing_project_rows(
    connection,
    *,
    encrypted_project_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not encrypted_project_ids:
        return {}

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                id,
                encrypted_project_id,
                registration_no,
                project_name,
                source_file,
                current_json_hash,
                raw_json IS NULL AS raw_json_missing,
                last_scraped_at,
                last_changed_at
            FROM rera_projects
            WHERE encrypted_project_id = ANY(%s)
            """,
            (encrypted_project_ids,),
        )
        rows = cursor.fetchall()
    return {row["encrypted_project_id"]: row for row in rows}


def select_detail_sync_candidates(
    connection,
    *,
    csv_by_id: dict[str, dict[str, Any]],
    json_dir: Path,
    max_projects: int,
    refresh_days: int,
    candidate_scan_multiplier: int,
    failure_state: dict[str, dict[str, Any]],
    failure_cooldown_hours: int,
) -> tuple[list[dict[str, Any]], int]:
    rows_by_id = load_existing_project_rows(
        connection,
        encrypted_project_ids=list(csv_by_id.keys()),
    )
    stale_cutoff = utcnow() - timedelta(days=refresh_days)
    failure_cutoff = utcnow() - timedelta(hours=failure_cooldown_hours)
    oldest = datetime(1970, 1, 1, tzinfo=timezone.utc)
    candidates: list[dict[str, Any]] = []
    suppressed_recent_failures = 0

    for encrypted_project_id, row in rows_by_id.items():
        failure_record = failure_state.get(encrypted_project_id) or {}
        last_failed_at_text = failure_record.get("last_failed_at")
        last_failed_at = None
        if isinstance(last_failed_at_text, str):
            try:
                last_failed_at = datetime.fromisoformat(last_failed_at_text)
            except ValueError:
                last_failed_at = None
        if last_failed_at is not None and last_failed_at >= failure_cutoff:
            suppressed_recent_failures += 1
            continue

        csv_row = csv_by_id.get(encrypted_project_id)
        output_path = build_json_output_path(
            json_dir=json_dir,
            encrypted_project_id=encrypted_project_id,
            csv_row=csv_row,
            existing_source_file=row.get("source_file"),
        )
        needs_backfill = (
            row.get("current_json_hash") is None
            or bool(row.get("raw_json_missing"))
            or not output_path.exists()
        )
        stale_refresh = (
            row.get("last_scraped_at") is None
            or row["last_scraped_at"] <= stale_cutoff
        )
        if not needs_backfill and not stale_refresh:
            continue

        candidates.append(
            {
                **row,
                "csv_row": csv_row,
                "target_file": output_path,
                "needs_backfill": needs_backfill,
                "stale_refresh": stale_refresh,
                "priority": 0 if needs_backfill else 1,
                "sort_last_scraped_at": row.get("last_scraped_at") or oldest,
            }
        )

    candidates.sort(
        key=lambda row: (
            row["priority"],
            row["sort_last_scraped_at"],
            row.get("registration_no") or "",
        )
    )
    scan_limit = max(max_projects * max(candidate_scan_multiplier, 1), max_projects)
    return candidates[:scan_limit], suppressed_recent_failures


def sync_listing_rows(
    connection,
    *,
    csv_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    processed = 0
    inserted = 0
    with connection.cursor() as cursor:
        for row in csv_rows:
            encrypted_project_id = (row.get("EncryptedProjectId") or "").strip()
            if not encrypted_project_id:
                continue
            was_inserted = upsert_listing_row(
                cursor,
                encrypted_project_id=encrypted_project_id,
                csv_row=row,
            )
            processed += 1
            if was_inserted:
                inserted += 1
    connection.commit()
    return processed, inserted


def sync_project_details(
    connection,
    *,
    api_key: str,
    csv_by_id: dict[str, dict[str, Any]],
    json_dir: Path,
    max_projects: int,
    refresh_days: int,
    candidate_scan_multiplier: int,
    failure_cooldown_hours: int,
    failure_state_path: Path,
) -> DetailSyncStats:
    stats = DetailSyncStats()
    failure_state = load_failure_state(failure_state_path)
    candidates, suppressed_recent_failures = select_detail_sync_candidates(
        connection,
        csv_by_id=csv_by_id,
        json_dir=json_dir,
        max_projects=max_projects,
        refresh_days=refresh_days,
        candidate_scan_multiplier=candidate_scan_multiplier,
        failure_state=failure_state,
        failure_cooldown_hours=failure_cooldown_hours,
    )
    stats.candidates_selected = len(candidates)
    stats.candidates_suppressed_recent_failures = suppressed_recent_failures
    if not candidates:
        return stats

    session = requests.Session()
    session.headers.update(build_rera_headers(api_key))

    for candidate in candidates:
        if stats.projects_fetched >= max_projects:
            break
        encrypted_project_id = candidate["encrypted_project_id"]
        stats.projects_attempted += 1
        try:
            raw_json, _bridge_payload = fetch_project_detail_json(
                session,
                encrypted_project_id=encrypted_project_id,
            )
            stats.projects_fetched += 1
            save_project_json(candidate["target_file"], raw_json)
            stats.json_files_saved += 1

            with connection.cursor() as cursor:
                result, snapshot_count, change_rows = ingest_json_file(
                    cursor,
                    encrypted_project_id=encrypted_project_id,
                    raw_json=raw_json,
                    csv_row=candidate.get("csv_row"),
                    source_file=str(candidate["target_file"]),
                    scraped_at=utcnow(),
                )
            connection.commit()

            stats.snapshots_inserted += snapshot_count
            stats.change_rows_inserted += change_rows
            if result == "inserted":
                stats.projects_inserted += 1
            elif result == "seeded":
                stats.projects_seeded += 1
            elif result == "changed":
                stats.projects_changed += 1
            elif result == "unchanged":
                stats.projects_unchanged += 1
            failure_state.pop(encrypted_project_id, None)
        except Exception as exc:  # noqa: BLE001
            connection.rollback()
            stats.projects_failed += 1
            stats.errors.append(f"{encrypted_project_id}: {exc}")
            failure_state[encrypted_project_id] = {
                "last_failed_at": utcnow().isoformat(),
                "error": str(exc),
                "registration_no": candidate.get("registration_no"),
                "project_name": candidate.get("project_name"),
                "fail_count": int((failure_state.get(encrypted_project_id) or {}).get("fail_count") or 0) + 1,
            }

    save_failure_state(failure_state_path, failure_state)

    return stats


def run_incremental_sync(
    connection,
    *,
    api_url: str,
    api_key: str,
    csv_path: Path,
    json_dir: Path,
    max_detail_projects: int,
    refresh_days: int,
    candidate_scan_multiplier: int,
    failure_cooldown_hours: int,
    failure_state_path: Path,
) -> SyncRunStats:
    rows = fetch_project_list(api_url, api_key)
    print(f"Total listing rows fetched from RERA API: {len(rows)}")
    write_csv(rows, csv_path)

    csv_by_id = {
        (row.get("EncryptedProjectId") or "").strip(): row
        for row in rows
        if (row.get("EncryptedProjectId") or "").strip()
    }
    processed, inserted = sync_listing_rows(
        connection,
        csv_rows=rows,
    )

    detail_stats = sync_project_details(
        connection,
        api_key=api_key,
        csv_by_id=csv_by_id,
        json_dir=json_dir,
        max_projects=max_detail_projects,
        refresh_days=refresh_days,
        candidate_scan_multiplier=candidate_scan_multiplier,
        failure_cooldown_hours=failure_cooldown_hours,
        failure_state_path=failure_state_path,
    )

    return SyncRunStats(
        csv_rows_fetched=len(rows),
        listing_rows_processed=processed,
        listing_rows_inserted=inserted,
        detail_sync=detail_stats,
    )
