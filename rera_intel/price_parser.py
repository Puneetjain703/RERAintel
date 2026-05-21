from __future__ import annotations

import re
from typing import Any


TOTAL_PRICE_RE = re.compile(
    r"(?P<text>(?:₹|rs\.?|inr)?\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>lac|lacs|lakh|lakhs|cr|crore|crores)\b)",
    re.IGNORECASE,
)
PLAIN_RUPEE_RE = re.compile(
    r"(?P<text>₹\s*(?P<number>\d[\d,]{4,}(?:\.\d+)?))",
    re.IGNORECASE,
)
PRICE_PER_SQFT_RE = re.compile(
    r"(?P<text>(?:₹|rs\.?|inr)?\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?:/|\s+per\s+)\s*(?:sq\.?\s*ft|sqft|sq\s*ft|square\s*feet?))",
    re.IGNORECASE,
)
PRICE_PER_SQYD_RE = re.compile(
    r"(?P<text>(?:₹|rs\.?|inr)?\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?:/|\s+per\s+)\s*(?:sq\.?\s*yd|sqyd|sq\s*yd|sq\s*yard|square\s*yards?))",
    re.IGNORECASE,
)


def parse_numeric_text(value: str) -> float:
    return float(value.replace(",", "").strip())


def parse_total_price(number_text: str, unit_text: str) -> float:
    number = parse_numeric_text(number_text)
    unit = unit_text.strip().lower()
    if unit in {"lac", "lacs", "lakh", "lakhs"}:
        return number * 100000
    if unit in {"cr", "crore", "crores"}:
        return number * 10000000
    return number


def spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def extract_price_observations(text: str) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    per_area_spans = [
        match.span()
        for regex in (PRICE_PER_SQFT_RE, PRICE_PER_SQYD_RE)
        for match in regex.finditer(text)
    ]

    for match in TOTAL_PRICE_RE.finditer(text):
        price_text = match.group("text").strip()
        key = ("total_price", price_text.lower())
        if key in seen:
            continue
        seen.add(key)
        observations.append(
            {
                "kind": "total_price",
                "text": price_text,
                "value": parse_total_price(match.group("number"), match.group("unit")),
            }
        )

    for match in PLAIN_RUPEE_RE.finditer(text):
        if any(spans_overlap(match.span(), span) for span in per_area_spans):
            continue
        price_text = match.group("text").strip()
        key = ("plain_rupee", price_text.lower())
        if key in seen:
            continue
        seen.add(key)
        observations.append(
            {
                "kind": "total_price",
                "text": price_text,
                "value": parse_numeric_text(match.group("number")),
            }
        )

    for match in PRICE_PER_SQFT_RE.finditer(text):
        price_text = match.group("text").strip()
        key = ("price_per_sqft", price_text.lower())
        if key in seen:
            continue
        seen.add(key)
        observations.append(
            {
                "kind": "price_per_sqft",
                "text": price_text,
                "value": parse_numeric_text(match.group("number")),
            }
        )

    for match in PRICE_PER_SQYD_RE.finditer(text):
        price_text = match.group("text").strip()
        key = ("price_per_sqyd", price_text.lower())
        if key in seen:
            continue
        seen.add(key)
        observations.append(
            {
                "kind": "price_per_sqyd",
                "text": price_text,
                "value": parse_numeric_text(match.group("number")),
            }
        )

    return observations


def summarize_price_observations(text: str) -> dict[str, Any]:
    observations = extract_price_observations(text)
    total_price = next((item for item in observations if item["kind"] == "total_price"), None)
    price_per_sqft = next((item for item in observations if item["kind"] == "price_per_sqft"), None)
    price_per_sqyd = next((item for item in observations if item["kind"] == "price_per_sqyd"), None)

    extracted_price = total_price or price_per_sqft or price_per_sqyd

    return {
        "matches": observations,
        "extracted_price_text": extracted_price["text"] if extracted_price else None,
        "extracted_price_value": total_price["value"] if total_price else None,
        "price_per_sqft": price_per_sqft["value"] if price_per_sqft else None,
        "price_per_sqyd": price_per_sqyd["value"] if price_per_sqyd else None,
    }
