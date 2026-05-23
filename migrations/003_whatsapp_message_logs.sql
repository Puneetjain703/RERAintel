CREATE TABLE IF NOT EXISTS whatsapp_message_logs (
    id BIGSERIAL PRIMARY KEY,
    logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound', 'system')),
    from_number TEXT,
    to_number TEXT,
    wa_message_id TEXT,
    question TEXT,
    answer TEXT,
    payload JSONB,
    status TEXT,
    error_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_message_logs_logged_at
    ON whatsapp_message_logs (logged_at DESC);

CREATE INDEX IF NOT EXISTS idx_whatsapp_message_logs_from_number
    ON whatsapp_message_logs (from_number, logged_at DESC);
