from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from .extraction import (
    derive_encrypted_project_id,
    diff_json_documents,
    extract_project_fields,
    serialize_project_fields,
    sha256_json,
)


PROJECT_FIELD_COLUMNS = [
    "registration_no",
    "project_name",
    "district_name",
    "promoter_name",
    "project_type",
    "application_no",
    "certificate_no",
    "project_status",
    "approved_on",
    "approved_year",
    "original_completion_date",
    "revised_completion_date",
    "actual_commencement_date",
    "tahsil_name",
    "village_name",
    "plot_no",
    "area_sqm",
    "phase_area_sqm",
    "saleable_area_sqm",
    "total_building_count",
    "sanctioned_building_count",
    "not_sanctioned_building_count",
]


@dataclass
class IngestStats:
    csv_rows_processed: int = 0
    csv_rows_skipped: int = 0
    json_files_processed: int = 0
    json_files_skipped: int = 0
    projects_inserted: int = 0
    snapshots_inserted: int = 0
    projects_changed: int = 0
    projects_unchanged: int = 0
    change_rows_inserted: int = 0
    errors: list[str] = field(default_factory=list)


def load_csv_rows(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def jsonb_or_none(value: Any):
    return Jsonb(value) if value is not None else None


def merge_value(new_value: Any, old_value: Any) -> Any:
    if new_value is None:
        return old_value
    if isinstance(new_value, str) and new_value.strip() == "":
        return old_value
    return new_value


def fetch_project(cursor, encrypted_project_id: str) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT * FROM rera_projects WHERE encrypted_project_id = %s",
        (encrypted_project_id,),
    )
    return cursor.fetchone()


def fetch_latest_snapshot(cursor, project_id: int) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT *
        FROM rera_project_snapshots
        WHERE project_id = %s
        ORDER BY scraped_at DESC, id DESC
        LIMIT 1
        """,
        (project_id,),
    )
    return cursor.fetchone()


def insert_snapshot(
    cursor,
    *,
    project_id: int,
    encrypted_project_id: str,
    json_hash: str,
    raw_json: dict[str, Any],
    extracted_fields: dict[str, Any],
    source_csv_row: dict[str, Any] | None,
    source_file: str | None,
    scraped_at: datetime,
) -> int:
    cursor.execute(
        """
        INSERT INTO rera_project_snapshots (
            project_id,
            encrypted_project_id,
            json_hash,
            raw_json,
            extracted_fields,
            source_csv_row,
            source_file,
            scraped_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            project_id,
            encrypted_project_id,
            json_hash,
            Jsonb(raw_json),
            Jsonb(extracted_fields),
            jsonb_or_none(source_csv_row),
            source_file,
            scraped_at,
        ),
    )
    snapshot_row = cursor.fetchone()
    return snapshot_row["id"]


def update_project_record(
    cursor,
    *,
    project_id: int,
    values: dict[str, Any],
) -> None:
    cursor.execute(
        """
        UPDATE rera_projects
        SET
            registration_no = %s,
            project_name = %s,
            district_name = %s,
            promoter_name = %s,
            project_type = %s,
            application_no = %s,
            certificate_no = %s,
            project_status = %s,
            approved_on = %s,
            approved_year = %s,
            original_completion_date = %s,
            revised_completion_date = %s,
            actual_commencement_date = %s,
            tahsil_name = %s,
            village_name = %s,
            plot_no = %s,
            area_sqm = %s,
            phase_area_sqm = %s,
            saleable_area_sqm = %s,
            total_building_count = %s,
            sanctioned_building_count = %s,
            not_sanctioned_building_count = %s,
            current_json_hash = %s,
            raw_json = %s,
            source_csv_row = %s,
            source_file = %s,
            csv_updated_on = %s,
            last_scraped_at = %s,
            last_changed_at = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            values["registration_no"],
            values["project_name"],
            values["district_name"],
            values["promoter_name"],
            values["project_type"],
            values["application_no"],
            values["certificate_no"],
            values["project_status"],
            values["approved_on"],
            values["approved_year"],
            values["original_completion_date"],
            values["revised_completion_date"],
            values["actual_commencement_date"],
            values["tahsil_name"],
            values["village_name"],
            values["plot_no"],
            values["area_sqm"],
            values["phase_area_sqm"],
            values["saleable_area_sqm"],
            values["total_building_count"],
            values["sanctioned_building_count"],
            values["not_sanctioned_building_count"],
            values["current_json_hash"],
            jsonb_or_none(values["raw_json"]),
            jsonb_or_none(values["source_csv_row"]),
            values["source_file"],
            values["csv_updated_on"],
            values["last_scraped_at"],
            values["last_changed_at"],
            project_id,
        ),
    )


def insert_project_record(
    cursor,
    *,
    encrypted_project_id: str,
    values: dict[str, Any],
) -> int:
    cursor.execute(
        """
        INSERT INTO rera_projects (
            encrypted_project_id,
            registration_no,
            project_name,
            district_name,
            promoter_name,
            project_type,
            application_no,
            certificate_no,
            project_status,
            approved_on,
            approved_year,
            original_completion_date,
            revised_completion_date,
            actual_commencement_date,
            tahsil_name,
            village_name,
            plot_no,
            area_sqm,
            phase_area_sqm,
            saleable_area_sqm,
            total_building_count,
            sanctioned_building_count,
            not_sanctioned_building_count,
            current_json_hash,
            raw_json,
            source_csv_row,
            source_file,
            csv_updated_on,
            last_scraped_at,
            last_changed_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        RETURNING id
        """,
        (
            encrypted_project_id,
            values["registration_no"],
            values["project_name"],
            values["district_name"],
            values["promoter_name"],
            values["project_type"],
            values["application_no"],
            values["certificate_no"],
            values["project_status"],
            values["approved_on"],
            values["approved_year"],
            values["original_completion_date"],
            values["revised_completion_date"],
            values["actual_commencement_date"],
            values["tahsil_name"],
            values["village_name"],
            values["plot_no"],
            values["area_sqm"],
            values["phase_area_sqm"],
            values["saleable_area_sqm"],
            values["total_building_count"],
            values["sanctioned_building_count"],
            values["not_sanctioned_building_count"],
            values["current_json_hash"],
            jsonb_or_none(values["raw_json"]),
            jsonb_or_none(values["source_csv_row"]),
            values["source_file"],
            values["csv_updated_on"],
            values["last_scraped_at"],
            values["last_changed_at"],
        ),
    )
    row = cursor.fetchone()
    return row["id"]


def build_project_values(
    existing_project: dict[str, Any] | None,
    extracted_fields: dict[str, Any],
    *,
    source_csv_row: dict[str, Any] | None,
    raw_json: dict[str, Any] | None,
    current_json_hash: str | None,
    source_file: str | None,
    last_scraped_at: datetime | None,
    last_changed_at: datetime | None,
) -> dict[str, Any]:
    base = existing_project or {}
    values: dict[str, Any] = {}

    for column in PROJECT_FIELD_COLUMNS:
        values[column] = merge_value(extracted_fields.get(column), base.get(column))

    values["current_json_hash"] = (
        current_json_hash if current_json_hash is not None else base.get("current_json_hash")
    )
    values["raw_json"] = raw_json if raw_json is not None else base.get("raw_json")
    values["source_csv_row"] = source_csv_row if source_csv_row is not None else base.get("source_csv_row")
    values["source_file"] = source_file if source_file is not None else base.get("source_file")
    values["csv_updated_on"] = merge_value(
        extracted_fields.get("csv_updated_on"),
        base.get("csv_updated_on"),
    )
    values["last_scraped_at"] = (
        last_scraped_at if last_scraped_at is not None else base.get("last_scraped_at")
    )
    values["last_changed_at"] = (
        last_changed_at if last_changed_at is not None else base.get("last_changed_at")
    )
    return values


def upsert_listing_row(
    cursor,
    *,
    encrypted_project_id: str,
    csv_row: dict[str, Any],
) -> bool:
    existing = fetch_project(cursor, encrypted_project_id)
    extracted_fields = extract_project_fields(None, csv_row)
    values = build_project_values(
        existing,
        extracted_fields,
        source_csv_row=csv_row,
        raw_json=None,
        current_json_hash=None,
        source_file=None,
        last_scraped_at=None,
        last_changed_at=None,
    )

    if existing is None:
        insert_project_record(
            cursor,
            encrypted_project_id=encrypted_project_id,
            values=values,
        )
        return True

    update_project_record(cursor, project_id=existing["id"], values=values)
    return False


def ingest_json_file(
    cursor,
    *,
    encrypted_project_id: str,
    raw_json: dict[str, Any],
    csv_row: dict[str, Any] | None,
    source_file: str,
    scraped_at: datetime,
) -> tuple[str, int, int]:
    existing = fetch_project(cursor, encrypted_project_id)
    extracted_fields = extract_project_fields(raw_json, csv_row)
    json_hash = sha256_json(raw_json)

    if existing is None:
        values = build_project_values(
            None,
            extracted_fields,
            source_csv_row=csv_row,
            raw_json=raw_json,
            current_json_hash=json_hash,
            source_file=source_file,
            last_scraped_at=scraped_at,
            last_changed_at=scraped_at,
        )
        project_id = insert_project_record(
            cursor,
            encrypted_project_id=encrypted_project_id,
            values=values,
        )
        insert_snapshot(
            cursor,
            project_id=project_id,
            encrypted_project_id=encrypted_project_id,
            json_hash=json_hash,
            raw_json=raw_json,
            extracted_fields=serialize_project_fields(extracted_fields),
            source_csv_row=csv_row,
            source_file=source_file,
            scraped_at=scraped_at,
        )
        return "inserted", 1, 0

    if not existing.get("current_json_hash"):
        values = build_project_values(
            existing,
            extracted_fields,
            source_csv_row=csv_row,
            raw_json=raw_json,
            current_json_hash=json_hash,
            source_file=source_file,
            last_scraped_at=scraped_at,
            last_changed_at=scraped_at,
        )
        insert_snapshot(
            cursor,
            project_id=existing["id"],
            encrypted_project_id=encrypted_project_id,
            json_hash=json_hash,
            raw_json=raw_json,
            extracted_fields=serialize_project_fields(extracted_fields),
            source_csv_row=csv_row,
            source_file=source_file,
            scraped_at=scraped_at,
        )
        update_project_record(cursor, project_id=existing["id"], values=values)
        return "seeded", 1, 0

    if existing.get("current_json_hash") == json_hash:
        values = build_project_values(
            existing,
            extracted_fields,
            source_csv_row=csv_row,
            raw_json=raw_json,
            current_json_hash=json_hash,
            source_file=source_file,
            last_scraped_at=scraped_at,
            last_changed_at=existing.get("last_changed_at"),
        )
        update_project_record(cursor, project_id=existing["id"], values=values)
        return "unchanged", 0, 0

    old_snapshot = fetch_latest_snapshot(cursor, existing["id"])
    if old_snapshot is None and existing.get("raw_json") is not None and existing.get("current_json_hash"):
        old_snapshot_id = insert_snapshot(
            cursor,
            project_id=existing["id"],
            encrypted_project_id=encrypted_project_id,
            json_hash=existing["current_json_hash"],
            raw_json=existing["raw_json"],
            extracted_fields=serialize_project_fields(
                extract_project_fields(existing["raw_json"], existing.get("source_csv_row"))
            ),
            source_csv_row=existing.get("source_csv_row"),
            source_file=existing.get("source_file"),
            scraped_at=existing.get("last_scraped_at") or existing.get("updated_at") or scraped_at,
        )
        old_snapshot = {
            "id": old_snapshot_id,
            "raw_json": existing["raw_json"],
        }

    new_snapshot_id = insert_snapshot(
        cursor,
        project_id=existing["id"],
        encrypted_project_id=encrypted_project_id,
        json_hash=json_hash,
        raw_json=raw_json,
        extracted_fields=serialize_project_fields(extracted_fields),
        source_csv_row=csv_row,
        source_file=source_file,
        scraped_at=scraped_at,
    )

    changes = diff_json_documents(
        old_snapshot.get("raw_json") if old_snapshot else None,
        raw_json,
    )
    if changes:
        cursor.executemany(
            """
            INSERT INTO rera_project_changes (
                project_id,
                encrypted_project_id,
                old_snapshot_id,
                new_snapshot_id,
                field_path,
                change_type,
                old_value,
                new_value,
                changed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    existing["id"],
                    encrypted_project_id,
                    old_snapshot["id"] if old_snapshot else None,
                    new_snapshot_id,
                    change["field_path"],
                    change["change_type"],
                    jsonb_or_none(change["old_value"]),
                    jsonb_or_none(change["new_value"]),
                    scraped_at,
                )
                for change in changes
            ],
        )

    values = build_project_values(
        existing,
        extracted_fields,
        source_csv_row=csv_row,
        raw_json=raw_json,
        current_json_hash=json_hash,
        source_file=source_file,
        last_scraped_at=scraped_at,
        last_changed_at=scraped_at,
    )
    update_project_record(cursor, project_id=existing["id"], values=values)
    return "changed", 1, len(changes)


def ingest_existing_data(
    connection,
    *,
    csv_path: Path,
    json_dir: Path,
) -> IngestStats:
    stats = IngestStats()
    scrape_time = utcnow()
    csv_rows = load_csv_rows(csv_path)
    csv_by_id: dict[str, dict[str, Any]] = {}

    with connection.cursor() as cursor:
        for row in csv_rows:
            encrypted_project_id = (row.get("EncryptedProjectId") or "").strip()
            if not encrypted_project_id:
                stats.csv_rows_skipped += 1
                continue

            csv_by_id[encrypted_project_id] = row
            inserted = upsert_listing_row(
                cursor,
                encrypted_project_id=encrypted_project_id,
                csv_row=row,
            )
            stats.csv_rows_processed += 1
            if inserted:
                stats.projects_inserted += 1

        known_ids = sorted(csv_by_id.keys(), key=len, reverse=True)
        json_files = sorted(json_dir.glob("*.json")) if json_dir.exists() else []

        for json_file in json_files:
            try:
                raw_json = json.loads(json_file.read_text(encoding="utf-8"))
                encrypted_project_id = derive_encrypted_project_id(
                    json_file,
                    raw_json,
                    known_ids=known_ids,
                )
                if not encrypted_project_id:
                    stats.json_files_skipped += 1
                    stats.errors.append(
                        f"Could not derive EncryptedProjectId from {json_file.name}"
                    )
                    continue

                result, snapshot_count, change_rows = ingest_json_file(
                    cursor,
                    encrypted_project_id=encrypted_project_id,
                    raw_json=raw_json,
                    csv_row=csv_by_id.get(encrypted_project_id),
                    source_file=str(json_file),
                    scraped_at=scrape_time,
                )
                stats.json_files_processed += 1
                stats.snapshots_inserted += snapshot_count
                stats.change_rows_inserted += change_rows
                if result == "changed":
                    stats.projects_changed += 1
                elif result == "unchanged":
                    stats.projects_unchanged += 1
                elif result == "inserted" and encrypted_project_id not in csv_by_id:
                    stats.projects_inserted += 1
            except Exception as exc:  # noqa: BLE001
                stats.json_files_skipped += 1
                stats.errors.append(f"{json_file.name}: {exc}")

    connection.commit()
    return stats
