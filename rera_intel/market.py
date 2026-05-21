from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from .roi_calculator import calculate_roi_metrics, round_amount


def calculate_price_per_sqft(
    *,
    price: float | None,
    area: float | None,
) -> float | None:
    if price is None or area is None or area <= 0:
        return None
    return round_amount(price / area)


def load_project_market_prices(connection, project_id: int) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                id,
                source,
                source_url,
                listing_title,
                price,
                area,
                price_per_sqft,
                notes,
                scraper_source,
                confidence_score,
                raw_data,
                is_manual,
                recorded_at,
                created_at
            FROM project_market_prices
            WHERE project_id = %s
            ORDER BY recorded_at DESC, id DESC
            """,
            (project_id,),
        )
        return cursor.fetchall()


def insert_project_market_price(
    connection,
    *,
    project_id: int,
    encrypted_project_id: str,
    source: str,
    source_url: str | None,
    listing_title: str | None,
    price: float | None,
    area: float | None,
    price_per_sqft: float | None,
    notes: str | None,
    scraper_source: str | None = "manual_entry",
    confidence_score: float | None = 1.0,
    raw_data: dict[str, Any] | None = None,
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO project_market_prices (
                project_id,
                encrypted_project_id,
                source,
                source_url,
                listing_title,
                price,
                area,
                price_per_sqft,
                notes,
                scraper_source,
                confidence_score,
                raw_data,
                is_manual
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            RETURNING id
            """,
            (
                project_id,
                encrypted_project_id,
                source,
                source_url,
                listing_title,
                round_amount(price),
                round_amount(area),
                round_amount(price_per_sqft),
                notes,
                scraper_source,
                confidence_score,
                Jsonb(raw_data) if raw_data is not None else None,
            ),
        )
        inserted_id = cursor.fetchone()["id"]
    connection.commit()
    return inserted_id


def load_project_roi_cases(connection, project_id: int) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                id,
                scenario_name,
                purchase_price,
                stamp_duty,
                registration,
                brokerage,
                other_cost,
                expected_sale_price,
                holding_period_months,
                total_investment,
                net_profit,
                roi_pct,
                annualized_roi_pct,
                created_at
            FROM project_roi_cases
            WHERE project_id = %s
            ORDER BY created_at DESC, id DESC
            """,
            (project_id,),
        )
        return cursor.fetchall()


def insert_project_roi_case(
    connection,
    *,
    project_id: int,
    encrypted_project_id: str,
    scenario_name: str | None,
    purchase_price: float,
    stamp_duty: float,
    registration: float,
    brokerage: float,
    other_cost: float,
    expected_sale_price: float,
    holding_period_months: int,
) -> tuple[int, dict[str, float | int | None]]:
    metrics = calculate_roi_metrics(
        purchase_price=purchase_price,
        stamp_duty=stamp_duty,
        registration=registration,
        brokerage=brokerage,
        other_cost=other_cost,
        expected_sale_price=expected_sale_price,
        holding_period_months=holding_period_months,
    )

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO project_roi_cases (
                project_id,
                encrypted_project_id,
                scenario_name,
                purchase_price,
                stamp_duty,
                registration,
                brokerage,
                other_cost,
                expected_sale_price,
                holding_period_months,
                total_investment,
                net_profit,
                roi_pct,
                annualized_roi_pct
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                project_id,
                encrypted_project_id,
                scenario_name,
                metrics["purchase_price"],
                metrics["stamp_duty"],
                metrics["registration"],
                metrics["brokerage"],
                metrics["other_cost"],
                metrics["expected_sale_price"],
                metrics["holding_period_months"],
                metrics["total_investment"],
                metrics["net_profit"],
                metrics["roi_pct"],
                metrics["annualized_roi_pct"],
            ),
        )
        inserted_id = cursor.fetchone()["id"]
    connection.commit()
    return inserted_id, metrics


def prepare_project_market_sync_payload(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": project["id"],
        "encrypted_project_id": project["encrypted_project_id"],
        "registration_no": project.get("registration_no"),
        "project_name": project.get("project_name"),
        "district_name": project.get("district_name"),
        "promoter_name": project.get("promoter_name"),
        "project_type": project.get("project_type"),
        "plot_no": project.get("plot_no"),
    }


def sync_project_market_prices(project: dict[str, Any]) -> list[dict[str, Any]]:
    raise NotImplementedError(
        "Automated discovery now lives in rera_intel.price_discovery. "
        "Use refresh_project_price_candidates() or run_weekly_price_sync()."
    )
