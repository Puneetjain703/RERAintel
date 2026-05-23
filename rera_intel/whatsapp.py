from __future__ import annotations

import json
import re
from typing import Any

import requests

from .ai_chat import clean_text, preserve_text, safe_ask_ai_chat
from .config import Settings, get_settings
from .db import get_connection


def normalize_phone_number(value: str | None) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if text.startswith("+"):
        return "+" + re.sub(r"\D+", "", text)
    return re.sub(r"\D+", "", text)


def is_allowed_whatsapp_number(number: str, settings: Settings) -> bool:
    allowed = {normalize_phone_number(item) for item in settings.whatsapp_allowed_numbers if item}
    if not allowed:
        return False
    normalized = normalize_phone_number(number)
    return normalized in allowed


def log_whatsapp_event(
    *,
    direction: str,
    from_number: str | None = None,
    to_number: str | None = None,
    wa_message_id: str | None = None,
    question: str | None = None,
    answer: str | None = None,
    payload: dict[str, Any] | None = None,
    status: str | None = None,
    error_text: str | None = None,
) -> None:
    settings = get_settings()
    try:
        with get_connection(settings.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO whatsapp_message_logs (
                        direction, from_number, to_number, wa_message_id, question, answer, payload, status, error_text
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        direction,
                        normalize_phone_number(from_number),
                        normalize_phone_number(to_number),
                        clean_text(wa_message_id) or None,
                        question,
                        answer,
                        json.dumps(payload or {}, ensure_ascii=False),
                        clean_text(status) or None,
                        error_text,
                    ),
                )
    except Exception:
        return


def extract_incoming_whatsapp_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            contacts = value.get("contacts") or []
            contact_name = ""
            if contacts and isinstance(contacts[0], dict):
                profile = contacts[0].get("profile") or {}
                contact_name = clean_text(profile.get("name"))
            metadata = value.get("metadata") or {}
            business_number = clean_text(metadata.get("display_phone_number"))
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                text_body = clean_text(((message.get("text") or {}).get("body")))
                messages.append(
                    {
                        "from_number": clean_text(message.get("from")),
                        "message_id": clean_text(message.get("id")),
                        "message_type": clean_text(message.get("type")),
                        "text": text_body,
                        "contact_name": contact_name,
                        "to_number": business_number,
                        "raw_message": message,
                    }
                )
    return messages


def build_whatsapp_reply(ai_result: dict[str, Any], *, max_chars: int) -> str:
    if ai_result.get("error"):
        return (
            "I could not answer that right now from the RERA intelligence system. "
            f"Error: {ai_result['error'][:300]}"
        )

    answer = preserve_text(ai_result.get("answer"))
    if answer and len(answer) <= max_chars:
        return answer

    top_rows = ai_result.get("data_preview_rows") or []
    summary_lines: list[str] = []
    if answer:
        first_block = answer.split("\n\n", 1)[0].strip()
        if not first_block:
            first_block = answer[:700].strip()
        summary_lines.append(first_block[:700].rstrip())

    project_lines: list[str] = []
    for row in top_rows[:5]:
        if not isinstance(row, dict):
            continue
        project_name = clean_text(
            row.get("project_name")
            or row.get("Project")
            or row.get("project")
        )
        registration = clean_text(row.get("registration_no") or row.get("Registration"))
        district = clean_text(row.get("district_name") or row.get("District"))
        if not any([project_name, registration, district]):
            continue
        parts = [part for part in [project_name, registration, district] if part]
        project_lines.append(f"- {' | '.join(parts)}")

    if project_lines:
        summary_lines.append("Top matching projects:")
        summary_lines.extend(project_lines)

    final_reply = "\n".join(line for line in summary_lines if line).strip()
    if not final_reply:
        final_reply = (answer or "No answer available right now.")[:max_chars].strip()

    if len(final_reply) > max_chars:
        final_reply = final_reply[: max_chars - 3].rstrip() + "..."
    return final_reply


def send_whatsapp_text_message(*, to_number: str, body: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.whatsapp_access_token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is missing.")
    if not settings.whatsapp_phone_number_id:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID is missing.")

    url = (
        f"https://graph.facebook.com/{settings.whatsapp_graph_api_version}/"
        f"{settings.whatsapp_phone_number_id}/messages"
    )
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        },
        json={
            "messaging_product": "whatsapp",
            "to": normalize_phone_number(to_number),
            "type": "text",
            "text": {
                "preview_url": False,
                "body": body,
            },
        },
        timeout=(20, 60),
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw_text": response.text}
    if response.status_code >= 400:
        raise RuntimeError(
            f"WhatsApp send failed with HTTP {response.status_code}: {json.dumps(payload, ensure_ascii=False)}"
        )
    return payload


def process_whatsapp_text_question(message: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    from_number = clean_text(message.get("from_number"))
    question = clean_text(message.get("text"))
    wa_message_id = clean_text(message.get("message_id"))

    log_whatsapp_event(
        direction="inbound",
        from_number=from_number,
        to_number=message.get("to_number"),
        wa_message_id=wa_message_id,
        question=question,
        payload=message,
        status="received",
    )

    if not is_allowed_whatsapp_number(from_number, settings):
        reply_text = "This WhatsApp bot is not enabled for this number yet."
        outbound_payload = send_whatsapp_text_message(to_number=from_number, body=reply_text)
        log_whatsapp_event(
            direction="outbound",
            from_number=message.get("to_number"),
            to_number=from_number,
            wa_message_id=clean_text((outbound_payload.get("messages") or [{}])[0].get("id")),
            question=question,
            answer=reply_text,
            payload=outbound_payload,
            status="blocked_not_allowlisted",
        )
        return {"ok": False, "status": "blocked_not_allowlisted"}

    if not question:
        reply_text = "Please send a text question for the RERA intelligence bot."
        outbound_payload = send_whatsapp_text_message(to_number=from_number, body=reply_text)
        log_whatsapp_event(
            direction="outbound",
            from_number=message.get("to_number"),
            to_number=from_number,
            wa_message_id=clean_text((outbound_payload.get("messages") or [{}])[0].get("id")),
            question=question,
            answer=reply_text,
            payload=outbound_payload,
            status="ignored_non_text",
        )
        return {"ok": False, "status": "ignored_non_text"}

    ai_result = safe_ask_ai_chat(question, "Auto", [])
    reply_text = build_whatsapp_reply(ai_result, max_chars=settings.whatsapp_reply_max_chars)
    outbound_payload = send_whatsapp_text_message(to_number=from_number, body=reply_text)
    log_whatsapp_event(
        direction="outbound",
        from_number=message.get("to_number"),
        to_number=from_number,
        wa_message_id=clean_text((outbound_payload.get("messages") or [{}])[0].get("id")),
        question=question,
        answer=reply_text,
        payload=outbound_payload,
        status="sent",
        error_text=ai_result.get("error"),
    )
    return {"ok": True, "status": "sent", "ai_result": ai_result, "reply_text": reply_text}
