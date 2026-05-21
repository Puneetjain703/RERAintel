from __future__ import annotations

from rera_intel.config import get_settings
from rera_intel.db import get_connection
from rera_intel.rera_sync import run_incremental_sync


def main() -> None:
    settings = get_settings(require_api_key=True)

    print("Fetching latest Rajasthan RERA project list, project raw JSON, and ingesting updates...")

    with get_connection(settings.database_url) as connection:
        stats = run_incremental_sync(
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

    print("Daily sync complete.")
    print(f"Fetched CSV rows: {stats.csv_rows_fetched}")
    print(f"Listing rows processed: {stats.listing_rows_processed}")
    print(f"New listing rows inserted: {stats.listing_rows_inserted}")
    print(f"Detail candidates selected: {stats.detail_sync.candidates_selected}")
    print(
        "Detail candidates suppressed by recent failure cooldown: "
        f"{stats.detail_sync.candidates_suppressed_recent_failures}"
    )
    print(f"Detail projects fetched: {stats.detail_sync.projects_fetched}")
    print(f"JSON files saved: {stats.detail_sync.json_files_saved}")
    print(f"Projects changed: {stats.detail_sync.projects_changed}")
    print(f"Projects unchanged: {stats.detail_sync.projects_unchanged}")
    print(f"Projects failed: {stats.detail_sync.projects_failed}")

    if stats.detail_sync.errors:
        print("\nWarnings:")
        for error in stats.detail_sync.errors[:20]:
            print(f"- {error}")


if __name__ == "__main__":
    main()
