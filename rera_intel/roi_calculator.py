from __future__ import annotations


def round_amount(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def calculate_roi_metrics(
    *,
    purchase_price: float,
    stamp_duty: float,
    registration: float,
    brokerage: float,
    other_cost: float,
    expected_sale_price: float,
    holding_period_months: int,
) -> dict[str, float | int | None]:
    total_investment = float(purchase_price + stamp_duty + registration + brokerage + other_cost)
    net_profit = float(expected_sale_price - total_investment)

    roi_pct: float | None = None
    annualized_roi_pct: float | None = None
    if total_investment > 0:
        roi_pct = (net_profit / total_investment) * 100

    if total_investment > 0 and expected_sale_price > 0 and holding_period_months > 0:
        gross_multiple = expected_sale_price / total_investment
        if gross_multiple > 0:
            annualized_roi_pct = ((gross_multiple ** (12 / holding_period_months)) - 1) * 100

    return {
        "purchase_price": round_amount(purchase_price),
        "stamp_duty": round_amount(stamp_duty),
        "registration": round_amount(registration),
        "brokerage": round_amount(brokerage),
        "other_cost": round_amount(other_cost),
        "expected_sale_price": round_amount(expected_sale_price),
        "holding_period_months": int(holding_period_months),
        "total_investment": round_amount(total_investment),
        "net_profit": round_amount(net_profit),
        "roi_pct": round_amount(roi_pct),
        "annualized_roi_pct": round_amount(annualized_roi_pct),
    }
