from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from rera_intel.ai_chat import safe_ask_ai_chat
from rera_intel.config import get_settings
from rera_intel.whatsapp import (
    extract_incoming_whatsapp_messages,
    process_whatsapp_text_question,
)


app = FastAPI(title="RERA Rajasthan Intelligence API", version="0.1.0")


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    mode: str = Field(default="Auto")
    history: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ask")
def ask(request: AskRequest) -> dict[str, Any]:
    return safe_ask_ai_chat(request.question.strip(), request.mode, request.history)


@app.get("/whatsapp/webhook")
def verify_whatsapp_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
):
    settings = get_settings()
    if not settings.whatsapp_verify_token:
        raise HTTPException(status_code=500, detail="WHATSAPP_VERIFY_TOKEN is missing.")
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="Webhook verification failed.")


@app.post("/whatsapp/webhook")
async def receive_whatsapp_webhook(request: Request):
    payload = await request.json()
    messages = extract_incoming_whatsapp_messages(payload)

    results: list[dict[str, Any]] = []
    for message in messages:
        if message.get("message_type") != "text":
            results.append(
                {
                    "ok": False,
                    "status": "ignored_non_text",
                    "message_id": message.get("message_id"),
                }
            )
            continue
        try:
            result = process_whatsapp_text_question(message)
            results.append(
                {
                    "ok": bool(result.get("ok")),
                    "status": result.get("status"),
                    "message_id": message.get("message_id"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "ok": False,
                    "status": "failed",
                    "message_id": message.get("message_id"),
                    "error": str(exc),
                }
            )

    return JSONResponse({"received": True, "processed": len(results), "results": results})
