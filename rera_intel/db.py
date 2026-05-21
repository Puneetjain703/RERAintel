from __future__ import annotations

from psycopg import connect
from psycopg.rows import dict_row


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rera_projects (
    id BIGSERIAL PRIMARY KEY,
    encrypted_project_id TEXT NOT NULL UNIQUE,
    registration_no TEXT,
    project_name TEXT,
    district_name TEXT,
    promoter_name TEXT,
    project_type TEXT,
    application_no TEXT,
    certificate_no TEXT,
    project_status TEXT,
    approved_on DATE,
    approved_year INTEGER,
    original_completion_date DATE,
    revised_completion_date DATE,
    actual_commencement_date DATE,
    tahsil_name TEXT,
    village_name TEXT,
    plot_no TEXT,
    area_sqm DOUBLE PRECISION,
    phase_area_sqm DOUBLE PRECISION,
    saleable_area_sqm DOUBLE PRECISION,
    total_building_count INTEGER,
    sanctioned_building_count INTEGER,
    not_sanctioned_building_count INTEGER,
    current_json_hash TEXT,
    raw_json JSONB,
    source_csv_row JSONB,
    source_file TEXT,
    csv_updated_on TIMESTAMPTZ,
    last_scraped_at TIMESTAMPTZ,
    last_changed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rera_project_snapshots (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES rera_projects(id) ON DELETE CASCADE,
    encrypted_project_id TEXT NOT NULL,
    json_hash TEXT NOT NULL,
    raw_json JSONB NOT NULL,
    extracted_fields JSONB NOT NULL,
    source_csv_row JSONB,
    source_file TEXT,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rera_project_changes (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES rera_projects(id) ON DELETE CASCADE,
    encrypted_project_id TEXT NOT NULL,
    old_snapshot_id BIGINT REFERENCES rera_project_snapshots(id) ON DELETE SET NULL,
    new_snapshot_id BIGINT REFERENCES rera_project_snapshots(id) ON DELETE SET NULL,
    field_path TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK (change_type IN ('added', 'removed', 'modified')),
    old_value JSONB,
    new_value JSONB,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_market_prices (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES rera_projects(id) ON DELETE CASCADE,
    encrypted_project_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT,
    listing_title TEXT,
    price DOUBLE PRECISION,
    area DOUBLE PRECISION,
    price_per_sqft DOUBLE PRECISION,
    notes TEXT,
    scraper_source TEXT,
    confidence_score DOUBLE PRECISION CHECK (
        confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)
    ),
    raw_data JSONB,
    is_manual BOOLEAN NOT NULL DEFAULT TRUE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_price_candidates (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES rera_projects(id) ON DELETE CASCADE,
    encrypted_project_id TEXT NOT NULL,
    registration_no TEXT,
    search_query TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT,
    result_title TEXT,
    result_snippet TEXT,
    extracted_price_text TEXT,
    extracted_price_value DOUBLE PRECISION,
    price_per_sqft DOUBLE PRECISION,
    price_per_sqyd DOUBLE PRECISION,
    confidence_score DOUBLE PRECISION CHECK (
        confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)
    ),
    match_reason TEXT,
    raw_result JSONB NOT NULL,
    scraper_source TEXT NOT NULL DEFAULT 'serpapi_google',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_roi_cases (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES rera_projects(id) ON DELETE CASCADE,
    encrypted_project_id TEXT NOT NULL,
    scenario_name TEXT,
    purchase_price DOUBLE PRECISION NOT NULL,
    stamp_duty DOUBLE PRECISION NOT NULL DEFAULT 0,
    registration DOUBLE PRECISION NOT NULL DEFAULT 0,
    brokerage DOUBLE PRECISION NOT NULL DEFAULT 0,
    other_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    expected_sale_price DOUBLE PRECISION NOT NULL,
    holding_period_months INTEGER NOT NULL,
    total_investment DOUBLE PRECISION NOT NULL,
    net_profit DOUBLE PRECISION NOT NULL,
    roi_pct DOUBLE PRECISION,
    annualized_roi_pct DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_chat_logs (
    id BIGSERIAL PRIMARY KEY,
    asked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode TEXT,
    model TEXT,
    question TEXT NOT NULL,
    answer TEXT,
    tool_trace JSONB,
    sql_queries JSONB,
    sources JSONB,
    error_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_rera_projects_registration_no
    ON rera_projects (registration_no);
CREATE INDEX IF NOT EXISTS idx_rera_projects_project_name
    ON rera_projects (project_name);
CREATE INDEX IF NOT EXISTS idx_rera_projects_district_name
    ON rera_projects (district_name);
CREATE INDEX IF NOT EXISTS idx_rera_projects_promoter_name
    ON rera_projects (promoter_name);
CREATE INDEX IF NOT EXISTS idx_rera_projects_project_type
    ON rera_projects (project_type);
CREATE INDEX IF NOT EXISTS idx_rera_projects_approved_year
    ON rera_projects (approved_year);
CREATE INDEX IF NOT EXISTS idx_rera_projects_last_changed_at
    ON rera_projects (last_changed_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_rera_project_snapshots_project_scraped_at
    ON rera_project_snapshots (project_id, scraped_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_rera_project_snapshots_hash
    ON rera_project_snapshots (encrypted_project_id, json_hash);
CREATE INDEX IF NOT EXISTS idx_rera_project_changes_project_changed_at
    ON rera_project_changes (project_id, changed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_rera_project_changes_encrypted_changed_at
    ON rera_project_changes (encrypted_project_id, changed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_project_market_prices_project_recorded_at
    ON project_market_prices (project_id, recorded_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_project_market_prices_encrypted_recorded_at
    ON project_market_prices (encrypted_project_id, recorded_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_project_market_prices_scraper_source
    ON project_market_prices (scraper_source);
CREATE INDEX IF NOT EXISTS idx_project_price_candidates_project_confidence
    ON project_price_candidates (project_id, confidence_score DESC NULLS LAST, id DESC);
CREATE INDEX IF NOT EXISTS idx_project_price_candidates_encrypted_confidence
    ON project_price_candidates (encrypted_project_id, confidence_score DESC NULLS LAST, id DESC);
CREATE INDEX IF NOT EXISTS idx_project_price_candidates_registration_no
    ON project_price_candidates (registration_no);
CREATE INDEX IF NOT EXISTS idx_project_roi_cases_project_created_at
    ON project_roi_cases (project_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_project_roi_cases_encrypted_created_at
    ON project_roi_cases (encrypted_project_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_ai_chat_logs_asked_at
    ON ai_chat_logs (asked_at DESC);
"""


def get_connection(database_url: str):
    return connect(database_url, row_factory=dict_row)


def create_schema(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(SCHEMA_SQL)
    connection.commit()
