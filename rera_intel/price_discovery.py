from __future__ import annotations

from typing import Any

import requests
from psycopg.types.json import Jsonb

from .price_matcher import detect_source_name, score_price_candidate
from .price_parser import summarize_price_observations


SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"


def generate_project_search_queries(project: dict[str, Any]) -> list[str]:
    project_name = (project.get("project_name") or "").strip()
    district_name = (project.get("district_name") or "").strip()
    promoter_name = (project.get("promoter_name") or "").strip()
    registration_no = (project.get("registration_no") or "").strip()

    queries = [
        f'"{project_name}" "{district_name}" price' if project_name and district_name else "",
        f'"{project_name}" "{promoter_name}" price' if project_name and promoter_name else "",
        f'"{project_name}" 99acres' if project_name else "",
        f'"{project_name}" Magicbricks' if project_name else "",
        f'"{project_name}" Housing' if project_name else "",
        f'"{registration_no}"' if registration_no else "",
    ]

    seen: set[str] = set()
    deduped = []
    for query in queries:
        cleaned = " ".join(query.split()).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def build_project_search_location(project: dict[str, Any], default_location: str) -> str:
    district_name = (project.get("district_name") or "").strip()
    if district_name:
        return f"{district_name}, Rajasthan, India"
    return default_location


def fetch_serpapi_results(
    *,
    api_key: str,
    query: str,
    location: str,
    gl: str,
    hl: str,
) -> dict[str, Any]:
    response = requests.get(
        SERPAPI_SEARCH_URL,
        params={
            "engine": "google",
            "api_key": api_key,
            "q": query,
            "location": location,
            "gl": gl,
            "hl": hl,
            "google_domain": "google.com",
            "num": 10,
        },
        timeout=(20, 60),
    )
    try:
        payload = response.json()
    except ValueError as exc:
        response.raise_for_status()
        raise RuntimeError("SerpAPI returned a non-JSON response.") from exc

    if response.status_code != 200:
        error_message = payload.get("error") or f"SerpAPI request failed with HTTP {response.status_code}"
        raise RuntimeError(error_message)

    search_status = str(payload.get("search_metadata", {}).get("status") or "").lower()
    if payload.get("error") and search_status == "error":
        raise RuntimeError(str(payload["error"]))

    return payload


def build_price_candidates_from_serp(
    *,
    project: dict[str, Any],
    search_query: str,
    serp_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for result in serp_payload.get("organic_results") or []:
        result_title = str(result.get("title") or "").strip()
        result_snippet = str(result.get("snippet") or "").strip()
        source_url = str(result.get("link") or "").strip() or None
        searchable_text = " ".join(part for part in [result_title, result_snippet] if part)
        price_summary = summarize_price_observations(searchable_text)
        if not price_summary["matches"]:
            continue

        scored = score_price_candidate(
            project=project,
            result_title=result_title,
            result_snippet=result_snippet,
            source_url=source_url,
        )
        candidates.append(
            {
                "registration_no": project.get("registration_no"),
                "search_query": search_query,
                "source": scored["source"] or detect_source_name(source_url),
                "source_url": source_url,
                "result_title": result_title or None,
                "result_snippet": result_snippet or None,
                "extracted_price_text": price_summary["extracted_price_text"],
                "extracted_price_value": price_summary["extracted_price_value"],
                "price_per_sqft": price_summary["price_per_sqft"],
                "price_per_sqyd": price_summary["price_per_sqyd"],
                "confidence_score": scored["confidence_score"],
                "match_reason": scored["match_reason"],
                "raw_result": {
                    "search_query": search_query,
                    "search_metadata": serp_payload.get("search_metadata"),
                    "search_information": serp_payload.get("search_information"),
                    "result": result,
                    "parsed_prices": price_summary["matches"],
                },
            }
        )
    return candidates


def dedupe_price_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for candidate in candidates:
        key = (
            candidate.get("source_url"),
            candidate.get("result_title"),
            candidate.get("extracted_price_text"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return sorted(
        deduped,
        key=lambda item: (
            -(item.get("confidence_score") or 0),
            -(item.get("extracted_price_value") or 0),
            -(item.get("price_per_sqft") or 0),
        ),
    )


def load_project_price_candidates(connection, project_id: int) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                id,
                registration_no,
                search_query,
                source,
                source_url,
                result_title,
                result_snippet,
                extracted_price_text,
                extracted_price_value,
                price_per_sqft,
                price_per_sqyd,
                confidence_score,
                match_reason,
                raw_result,
                scraper_source,
                created_at
            FROM project_price_candidates
            WHERE project_id = %s
            ORDER BY confidence_score DESC NULLS LAST, id DESC
            """,
            (project_id,),
        )
        return cursor.fetchall()


def replace_project_price_candidates(
    connection,
    *,
    project: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM project_price_candidates
            WHERE project_id = %s
              AND scraper_source = 'serpapi_google'
            """,
            (project["id"],),
        )
        for candidate in candidates:
            cursor.execute(
                """
                INSERT INTO project_price_candidates (
                    project_id,
                    encrypted_project_id,
                    registration_no,
                    search_query,
                    source,
                    source_url,
                    result_title,
                    result_snippet,
                    extracted_price_text,
                    extracted_price_value,
                    price_per_sqft,
                    price_per_sqyd,
                    confidence_score,
                    match_reason,
                    raw_result,
                    scraper_source
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'serpapi_google')
                """,
                (
                    project["id"],
                    project["encrypted_project_id"],
                    candidate.get("registration_no"),
                    candidate.get("search_query"),
                    candidate.get("source"),
                    candidate.get("source_url"),
                    candidate.get("result_title"),
                    candidate.get("result_snippet"),
                    candidate.get("extracted_price_text"),
                    candidate.get("extracted_price_value"),
                    candidate.get("price_per_sqft"),
                    candidate.get("price_per_sqyd"),
                    candidate.get("confidence_score"),
                    candidate.get("match_reason"),
                    Jsonb(candidate["raw_result"]),
                ),
            )
    connection.commit()
    return len(candidates)


def refresh_project_price_candidates(
    connection,
    *,
    project: dict[str, Any],
    api_key: str,
    gl: str,
    hl: str,
    default_location: str,
) -> list[dict[str, Any]]:
    search_queries = generate_project_search_queries(project)
    location = build_project_search_location(project, default_location)
    candidates: list[dict[str, Any]] = []
    for query in search_queries:
        serp_payload = fetch_serpapi_results(
            api_key=api_key,
            query=query,
            location=location,
            gl=gl,
            hl=hl,
        )
        candidates.extend(
            build_price_candidates_from_serp(
                project=project,
                search_query=query,
                serp_payload=serp_payload,
            )
        )

    candidates = dedupe_price_candidates(candidates)
    replace_project_price_candidates(
        connection,
        project=project,
        candidates=candidates,
    )
    return candidates


def load_projects_for_price_sync(connection, *, limit: int) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM rera_projects
            ORDER BY COALESCE(last_changed_at, last_scraped_at, created_at) DESC NULLS LAST, id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cursor.fetchall()


def run_weekly_price_sync(
    connection,
    *,
    api_key: str,
    gl: str,
    hl: str,
    default_location: str,
    limit: int,
) -> dict[str, Any]:
    projects = load_projects_for_price_sync(connection, limit=limit)
    total_candidates = 0
    project_summaries = []
    for project in projects:
        candidates = refresh_project_price_candidates(
            connection,
            project=project,
            api_key=api_key,
            gl=gl,
            hl=hl,
            default_location=default_location,
        )
        total_candidates += len(candidates)
        project_summaries.append(
            {
                "encrypted_project_id": project["encrypted_project_id"],
                "project_name": project.get("project_name"),
                "candidate_count": len(candidates),
            }
        )
    return {
        "projects_processed": len(projects),
        "total_candidates": total_candidates,
        "projects": project_summaries,
    }


def resolve_selected_market_price(
    *,
    price_candidates: list[dict[str, Any]],
    manual_prices: list[dict[str, Any]],
) -> dict[str, Any] | None:
    manual_override = next(
        (
            row
            for row in manual_prices
            if row.get("price") is not None or row.get("price_per_sqft") is not None
        ),
        None,
    )
    if manual_override:
        return {
            "selection_type": "manual_override",
            "price": manual_override.get("price"),
            "price_per_sqft": manual_override.get("price_per_sqft"),
            "price_per_sqyd": None,
            "source": manual_override.get("source"),
            "source_url": manual_override.get("source_url"),
            "confidence_score": manual_override.get("confidence_score"),
            "label": "Manual override",
        }

    auto_candidate = next(
        (
            row
            for row in price_candidates
            if row.get("extracted_price_value") is not None
            or row.get("price_per_sqft") is not None
            or row.get("price_per_sqyd") is not None
        ),
        None,
    )
    if not auto_candidate:
        return None

    return {
        "selection_type": "automatic_candidate",
        "price": auto_candidate.get("extracted_price_value"),
        "price_per_sqft": auto_candidate.get("price_per_sqft"),
        "price_per_sqyd": auto_candidate.get("price_per_sqyd"),
        "source": auto_candidate.get("source"),
        "source_url": auto_candidate.get("source_url"),
        "confidence_score": auto_candidate.get("confidence_score"),
        "label": "Top automatic candidate",
    }
