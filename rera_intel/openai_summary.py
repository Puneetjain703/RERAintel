from __future__ import annotations

import base64
import mimetypes
import re
from typing import Any
from urllib.parse import urlparse

import requests

from .documents import probe_document
from .maps import load_map_document_text


def download_remote_document(url: str) -> dict[str, Any]:
    response = requests.get(url, allow_redirects=True, timeout=(20, 120))
    response.raise_for_status()
    content_type = response.headers.get("Content-Type") or ""
    return {
        "content": response.content,
        "content_type": content_type.split(";")[0].strip().lower(),
        "final_url": response.url,
    }


def build_upload_filename(title: str, url: str, content_type: str | None = None) -> str:
    parsed = urlparse(url)
    basename = parsed.path.rsplit("/", 1)[-1].strip()
    if basename and "." in basename:
        return basename

    safe_title = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._")
    safe_title = safe_title or "document"
    extension = mimetypes.guess_extension(content_type or "") or ""
    if extension == ".jpe":
        extension = ".jpg"
    return f"{safe_title}{extension}"


def to_data_url(content_type: str, content: bytes) -> str:
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def summarize_remote_document(
    *,
    url: str,
    title: str,
    api_key: str,
    model: str,
) -> str:
    from openai import OpenAI

    probe = probe_document(url)
    content_type = probe.get("content_type") or mimetypes.guess_type(url)[0] or ""
    content_type = content_type.split(";")[0].strip().lower()
    client = OpenAI(api_key=api_key)

    if content_type == "application/pdf":
        downloaded = download_remote_document(url)
        upload = client.files.create(
            file=(
                build_upload_filename(title, downloaded["final_url"], downloaded["content_type"] or content_type),
                downloaded["content"],
                downloaded["content_type"] or content_type or "application/pdf",
            ),
            purpose="user_data",
            expires_after={
                "anchor": "created_at",
                "seconds": 3600,
            },
        )
        document_input: dict[str, Any] = {
            "type": "input_file",
            "file_id": upload.id,
        }
    elif content_type.startswith("image/"):
        downloaded = download_remote_document(url)
        document_input = {
            "type": "input_image",
            "image_url": to_data_url(
                downloaded["content_type"] or content_type,
                downloaded["content"],
            ),
        }
    elif content_type in {
        "application/vnd.google-earth.kml+xml",
        "application/vnd.google-earth.kmz",
    } or url.lower().split("?", 1)[0].endswith((".kml", ".kmz")):
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Read this Rajasthan RERA KML or KMZ location file and provide a concise summary. "
                                "Use short sections titled: Map summary, Visible geometry, Coordinate clues, "
                                "and Red flags or uncertainties. Mention whether it looks like a point, boundary, "
                                "or route, and if the geometry appears incomplete say so."
                                f"\n\nDocument title: {title}\n\nKML content:\n{load_map_document_text(url)[:40000]}"
                            ),
                        }
                    ],
                }
            ],
        )
        return response.output_text
    else:
        raise ValueError(
            f"Unsupported document type for OpenAI summary: {content_type or 'unknown'}"
        )

    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Read this Rajasthan RERA project document and provide a concise summary. "
                            "Use short sections titled: Summary, Key facts, Compliance or legal points, "
                            "and Red flags or missing information. If the document is unreadable, say so."
                            f"\n\nDocument title: {title}"
                        ),
                    },
                    document_input,
                ],
            }
        ],
    )

    return response.output_text
