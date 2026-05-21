from __future__ import annotations

from typing import Any

import requests


def _build_headers(user_agent: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": user_agent,
    }


def geocode_query(
    *,
    endpoint: str,
    user_agent: str,
    query: str,
    email: str | None = None,
    country_codes: str = "in",
    limit: int = 1,
) -> dict[str, Any] | None:
    params: dict[str, Any] = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
        "countrycodes": country_codes,
    }
    if email:
        params["email"] = email

    response = requests.get(
        endpoint,
        params=params,
        headers=_build_headers(user_agent),
        timeout=30,
    )
    response.raise_for_status()
    results = response.json()
    if not isinstance(results, list) or not results:
        return None
    return results[0]


def reverse_geocode_coordinates(
    *,
    endpoint: str,
    user_agent: str,
    latitude: float,
    longitude: float,
    email: str | None = None,
) -> dict[str, Any] | None:
    params: dict[str, Any] = {
        "lat": latitude,
        "lon": longitude,
        "format": "jsonv2",
        "addressdetails": 1,
        "zoom": 18,
    }
    if email:
        params["email"] = email

    response = requests.get(
        endpoint,
        params=params,
        headers=_build_headers(user_agent),
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    return result if isinstance(result, dict) else None
