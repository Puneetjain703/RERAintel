from __future__ import annotations

from rera_intel.config import get_settings
from rera_intel.db import create_schema, get_connection


def main() -> None:
    settings = get_settings()
    with get_connection(settings.database_url) as connection:
        create_schema(connection)
    print("Database schema is ready.")
    print(
        "Tables: rera_projects, rera_project_snapshots, rera_project_changes, "
        "project_market_prices, project_price_candidates, project_roi_cases"
    )


if __name__ == "__main__":
    main()
