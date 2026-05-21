from __future__ import annotations

from rera_intel.config import get_settings
from rera_intel.db import get_connection
from rera_intel.ingest import ingest_existing_data


def main() -> None:
    settings = get_settings()

    with get_connection(settings.database_url) as connection:
        stats = ingest_existing_data(
            connection,
            csv_path=settings.csv_path,
            json_dir=settings.json_dir,
        )

    print("Ingestion complete.")
    print(f"CSV rows processed: {stats.csv_rows_processed}")
    print(f"CSV rows skipped: {stats.csv_rows_skipped}")
    print(f"JSON files processed: {stats.json_files_processed}")
    print(f"JSON files skipped: {stats.json_files_skipped}")
    print(f"Projects inserted: {stats.projects_inserted}")
    print(f"Projects changed: {stats.projects_changed}")
    print(f"Projects unchanged: {stats.projects_unchanged}")
    print(f"Snapshots inserted: {stats.snapshots_inserted}")
    print(f"Change rows inserted: {stats.change_rows_inserted}")

    if stats.errors:
        print("\nWarnings:")
        for error in stats.errors[:20]:
            print(f"- {error}")
        if len(stats.errors) > 20:
            print(f"- ... and {len(stats.errors) - 20} more")


if __name__ == "__main__":
    main()

