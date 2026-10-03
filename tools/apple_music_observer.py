from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from mitmproxy import http


ROOT = Path(os.environ.get("APPLE_MUSIC_RESEARCH_DIR", Path(__file__).resolve().parents[1]))
LABEL = os.environ.get("APPLE_MUSIC_SESSION_LABEL", "unlabeled")
TARGETS = [x.strip() for x in os.environ.get("APPLE_MUSIC_TARGETS", "Album title").split("|") if x.strip()]
EVENT_FILE = ROOT / "observations" / f"{LABEL}.jsonl"
WRITE_HOLD_FILE = ROOT / "observations" / "library-write-hold.enabled"

SAFE_HEADERS = {
    "accept",
    "accept-encoding",
    "accept-language",
    "content-encoding",
    "content-language",
    "content-length",
    "content-type",
    "user-agent",
    "x-apple-client-info",
    "x-apple-i-locale",
    "x-apple-language",
    "x-apple-locale",
    "x-apple-store-front",
    "x-apple-storefront",
}
KEYWORDS = (
    "album",
    "artist",
    "composer",
    "localization",
    "locale",
    "language",
    "library",
    "cloud",
    "update",
    "sync",
)


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def digest(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()[:16]


def protected(value: str) -> str:
    return f"<redacted len={len(value)} sha256={digest(value)}>"


def clean_path(path: str) -> str:
    path = re.sub(r"(?i)\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", path)
    path = re.sub(r"(?<=/)[0-9]{10,}(?=/|$)", "<numeric-id>", path)
    return path


def headers_summary(headers: http.Headers) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items(multi=True):
        key = name.lower()
        if key in SAFE_HEADERS or "locale" in key or "language" in key or "store-front" in key:
            result[key] = value
        else:
            result[key] = protected(value)
    return result


def json_paths(value: Any, prefix: str = "$", depth: int = 0) -> list[str]:
    if depth > 5:
        return []
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in list(value.items())[:100]:
            path = f"{prefix}.{key}"
            paths.append(path)
            paths.extend(json_paths(child, path, depth + 1))
    elif isinstance(value, list) and value:
        paths.extend(json_paths(value[0], f"{prefix}[]", depth + 1))
    return paths[:500]


def body_summary(content: bytes | None, content_type: str) -> dict[str, Any]:
    data = content or b""
    summary: dict[str, Any] = {"length": len(data), "sha256": digest(data)}
    if not data:
        return summary

    textual = any(token in content_type.lower() for token in ("json", "text", "xml", "plist", "form"))
    textual = textual or data[:1] in (b"{", b"[", b"<")
    if not textual:
        return summary

    text = data.decode("utf-8", "replace")
    lowered = text.lower()
    summary["target_hits"] = [target for target in TARGETS if target.lower() in lowered]
    summary["keyword_hits"] = [keyword for keyword in KEYWORDS if keyword in lowered]
    if "json" in content_type.lower() or data[:1] in (b"{", b"["):
        try:
            summary["json_paths"] = json_paths(json.loads(text))
        except (ValueError, TypeError):
            summary["json_parse"] = "failed"
    return summary


def append(record: dict[str, Any]) -> None:
    EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {"time": now(), "session": LABEL, **record}
    with EVENT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def classify(host: str | None, path: str, method: str, body: dict[str, Any]) -> tuple[str, list[str]]:
    host = (host or "").lower()
    lowered_path = path.lower()
    if host == "xp.apple.com":
        return "telemetry", []
    if "fpinit" in host or "/commerce/device/" in lowered_path:
        return "auth_or_device_registration", []
    if "librarydaap" in host and lowered_path.endswith("/server-info"):
        return "library_handshake", []
    if "librarydaap" in host and lowered_path.endswith("/update"):
        return "library_revision_poll", []
    if "librarydaap" in host and lowered_path.endswith("/pins"):
        return "library_read", []
    if "upp.itunes" in host and lowered_path.endswith("/getall"):
        return "account_domain_read", []
    if "pd.itunes" in host and lowered_path.endswith("/update"):
        return "purchase_revision_poll", []
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "read", []
    reasons = [f"method:{method}"]
    if body.get("keyword_hits"):
        reasons.append("metadata-keyword-in-request")
    if body.get("target_hits"):
        reasons.append("target-title-in-request")
    return "unknown_mutation", reasons


def request(flow: http.HTTPFlow) -> None:
    split = urlsplit(flow.request.pretty_url)
    query_keys = sorted({key for key, _ in parse_qsl(split.query, keep_blank_values=True)})
    request_body = body_summary(flow.request.raw_content, flow.request.headers.get("content-type", ""))
    method = flow.request.method.upper()
    category, reasons = classify(split.hostname, split.path, method, request_body)
    flow.metadata["research_request_body"] = request_body
    hold_library_write = (
        WRITE_HOLD_FILE.exists()
        and (split.hostname or "").lower() == "librarydaap.itunes.apple.com"
        and split.path.lower().endswith("/edit")
    )
    append(
        {
            "event": "request",
            "flow_id": flow.id,
            "method": method,
            "scheme": split.scheme,
            "host": split.hostname,
            "port": split.port,
            "path": clean_path(split.path),
            "query_keys": query_keys,
            "http_version": flow.request.http_version,
            "headers": headers_summary(flow.request.headers),
            "body": request_body,
            "category": category,
            "write_candidate": bool(reasons),
            "write_reasons": reasons,
            "held_locally": hold_library_write,
        }
    )
    if hold_library_write:
        append(
            {
                "event": "library_write_held",
                "flow_id": flow.id,
                "method": method,
                "host": split.hostname,
                "path": clean_path(split.path),
                "reason": "local-first repair mode",
            }
        )
        flow.response = http.Response.make(
            503,
            b"Library write held locally for controlled repair",
            {"Content-Type": "text/plain", "X-Apple-Music-Fixer": "write-held"},
        )


def response(flow: http.HTTPFlow) -> None:
    split = urlsplit(flow.request.pretty_url)
    response_body = body_summary(flow.response.raw_content, flow.response.headers.get("content-type", ""))
    append(
        {
            "event": "response",
            "flow_id": flow.id,
            "method": flow.request.method.upper(),
            "host": split.hostname,
            "path": clean_path(split.path),
            "status": flow.response.status_code,
            "headers": headers_summary(flow.response.headers),
            "body": response_body,
            "request_body": flow.metadata.get("research_request_body", {}),
            "duration_ms": round((flow.response.timestamp_end - flow.request.timestamp_start) * 1000, 1)
            if flow.response.timestamp_end
            else None,
        }
    )


def error(flow: http.HTTPFlow) -> None:
    split = urlsplit(flow.request.pretty_url) if flow.request else None
    append(
        {
            "event": "error",
            "flow_id": flow.id,
            "method": flow.request.method.upper() if flow.request else None,
            "host": split.hostname if split else None,
            "path": clean_path(split.path) if split else None,
            "message": str(flow.error),
        }
    )
