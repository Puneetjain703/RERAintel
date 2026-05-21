from __future__ import annotations

import mimetypes
import re
from typing import Any
from urllib.parse import urljoin

import requests


DOCUMENT_BASE_URL = "https://reraapp.rajasthan.gov.in/"
FILE_EXTENSIONS_RE = re.compile(
    r"\.(pdf|png|jpg|jpeg|gif|bmp|webp|tif|tiff|doc|docx|xls|xlsx|csv|txt|kml|kmz)$",
    re.IGNORECASE,
)
MAP_MIME_TYPES = {
    "application/vnd.google-earth.kml+xml",
    "application/vnd.google-earth.kmz",
}


def resolve_document_url(path: Any) -> str | None:
    if not path:
        return None

    text = str(path).strip()
    if not text or text in {"NA", "None", "null"}:
        return None

    if text.startswith("http://") or text.startswith("https://"):
        return text

    text = text.replace("~/", "").replace("../", "")
    text = text.lstrip("/")
    return urljoin(DOCUMENT_BASE_URL, text)


def looks_like_file_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if text.startswith(("http://", "https://", "~/", "../", "/")):
        return True
    return bool(FILE_EXTENSIONS_RE.search(text))


def add_document_record(
    records: list[dict[str, Any]],
    *,
    section: str,
    title: str,
    path: Any,
    filename: Any = None,
    document_type: str | None = None,
    source_field: str | None = None,
) -> None:
    url = resolve_document_url(path)
    if not url:
        return

    label = str(title or filename or source_field or "Document").strip()
    label = label or "Document"
    file_name = str(filename or "").strip() or url.rstrip("/").split("/")[-1]
    guessed_mime, _ = mimetypes.guess_type(url)
    lower_url = url.lower().split("?", 1)[0]
    if guessed_mime in MAP_MIME_TYPES or lower_url.endswith((".kml", ".kmz")):
        document_kind = "map"
    elif guessed_mime and guessed_mime.startswith("image/"):
        document_kind = "image"
    elif guessed_mime == "application/pdf":
        document_kind = "pdf"
    else:
        document_kind = "other"

    records.append(
        {
            "section": section,
            "title": label,
            "file_name": file_name,
            "document_type": document_type or section,
            "source_field": source_field,
            "path": str(path),
            "url": url,
            "content_type": guessed_mime,
            "document_kind": document_kind,
        }
    )


def collect_project_documents(
    raw_json: dict[str, Any] | None,
    source_csv_row: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    raw_json = raw_json or {}
    source_csv_row = source_csv_row or {}
    records: list[dict[str, Any]] = []

    for item in raw_json.get("GetDocumentsList") or []:
        if not isinstance(item, dict):
            continue
        add_document_record(
            records,
            section="GetDocumentsList",
            title=item.get("ApplicationDocumentName") or item.get("DocumentName"),
            path=item.get("DocumentUrl") or item.get("FilePath"),
            filename=item.get("DocumentName"),
            document_type=item.get("MasterType"),
            source_field="DocumentUrl",
        )

    project_documents = raw_json.get("ProjectDocuments")
    if isinstance(project_documents, dict):
        add_document_record(
            records,
            section="ProjectDocuments",
            title=project_documents.get("DocumentName") or "Project document",
            path=project_documents.get("DocumentUrl") or project_documents.get("DocumentPath"),
            filename=project_documents.get("DocumentName"),
            document_type="ProjectDocuments",
            source_field="DocumentUrl",
        )
        for nested_key in ["GetDocumentsList", "IndexDocumentsList"]:
            nested_list = project_documents.get(nested_key)
            if not isinstance(nested_list, list):
                continue
            for item in nested_list:
                if not isinstance(item, dict):
                    continue
                add_document_record(
                    records,
                    section=f"ProjectDocuments.{nested_key}",
                    title=item.get("ApplicationDocumentName") or item.get("DocumentName"),
                    path=item.get("DocumentUrl") or item.get("FilePath"),
                    filename=item.get("DocumentName"),
                    document_type=item.get("MasterType") or nested_key,
                    source_field="DocumentUrl",
                )

    for item in raw_json.get("PromoterDocumentList") or []:
        if not isinstance(item, dict):
            continue
        for field_name, value in item.items():
            if looks_like_file_path(value):
                add_document_record(
                    records,
                    section="PromoterDocumentList",
                    title=field_name,
                    path=value,
                    filename=None,
                    document_type="Promoter document",
                    source_field=field_name,
                )

    uploaded_certificate_path = source_csv_row.get("UploadedCertificatePath")
    if looks_like_file_path(uploaded_certificate_path):
        add_document_record(
            records,
            section="CSV",
            title="Uploaded certificate",
            path=uploaded_certificate_path,
            filename=None,
            document_type="Certificate",
            source_field="UploadedCertificatePath",
        )

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (record["url"], record["title"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)

    return deduped


def probe_document(url: str) -> dict[str, Any]:
    response = requests.head(url, allow_redirects=True, timeout=20)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type")
    content_length = response.headers.get("Content-Length")
    return {
        "content_type": content_type,
        "content_length": int(content_length) if content_length and content_length.isdigit() else None,
        "final_url": response.url,
    }
