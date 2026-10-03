from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from mitmproxy import http


OUTPUT = Path(os.environ.get("APPLE_MUSIC_DETAIL_OUT", "sync-details.json"))
HOST_MARKERS = ("librarydaap", "upp.itunes", "sync.itunes", "purchasedaap", "pd.itunes")
SAFE_VALUE_KEYS = {
    "accept-language",
    "client-version",
    "delta",
    "groupType",
    "language",
    "locale",
    "meta",
    "media-kind",
    "query",
    "revision-number",
    "sync-anchor",
    "type",
    "x-apple-i-locale",
    "x-apple-language",
    "x-apple-locale",
    "x-apple-store-front",
}
CONTAINER_TAGS = {"mccr", "mupd", "avdb", "mlcl", "mlit", "msrv", "mdcl", "adbs", "apso", "aply"}


def sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8", "replace")
    return hashlib.sha256(value).hexdigest()[:16]


def mask(key: str, value: Any) -> Any:
    lowered = key.lower()
    if lowered in SAFE_VALUE_KEYS or any(x in lowered for x in ("locale", "language", "revision")):
        return value
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8", "replace")
    return {"type": type(value).__name__, "length": len(raw), "sha256": sha(raw)}


def summarize(value: Any, key: str = "root", depth: int = 0) -> Any:
    if depth > 6:
        return "<depth-limit>"
    if isinstance(value, dict):
        return {str(k): summarize(v, str(k), depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [summarize(v, key, depth + 1) for v in value[:10]]
    return mask(key, value)


def parse_dmap(data: bytes, depth: int = 0) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while offset + 8 <= len(data) and len(items) < 100:
        raw_tag = data[offset : offset + 4]
        try:
            tag = raw_tag.decode("ascii")
        except UnicodeDecodeError:
            break
        length = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        end = offset + 8 + length
        if end > len(data):
            items.append({"tag": tag, "declared_length": length, "parse_error": "truncated"})
            break
        payload = data[offset + 8 : end]
        item: dict[str, Any] = {"tag": tag, "length": length}
        nested_tlv = False
        if len(payload) >= 8:
            nested_tag = payload[:4]
            nested_length = struct.unpack(">I", payload[4:8])[0]
            nested_tlv = all(32 <= byte < 127 for byte in nested_tag) and nested_length <= len(payload) - 8
        if (tag in CONTAINER_TAGS or nested_tlv) and depth < 8:
            item["children"] = parse_dmap(payload, depth + 1)
        elif length in (1, 2, 4, 8):
            item["integer"] = int.from_bytes(payload, "big")
        elif payload and all(byte in b"\t\r\n" or 32 <= byte < 127 for byte in payload):
            item["text"] = payload.decode("utf-8", "replace")
        else:
            item["sha256"] = sha(payload)
        items.append(item)
        offset = end
    if offset < len(data):
        items.append({"unparsed_length": len(data) - offset, "sha256": sha(data[offset:])})
    return items


def body_detail(content: bytes | None, content_type: str) -> dict[str, Any]:
    data = content or b""
    result: dict[str, Any] = {"length": len(data), "sha256": sha(data), "content_type": content_type}
    lowered = content_type.lower()
    if not data:
        return result
    if "x-www-form-urlencoded" in lowered:
        text = data.decode("utf-8", "replace")
        result["form"] = {key: mask(key, value) for key, value in parse_qsl(text, keep_blank_values=True)}
    elif "plist" in lowered or data.startswith(b"bplist") or data.lstrip().startswith(b"<?xml"):
        if data.startswith(b"bplist"):
            candidates = []
            for token in re.findall(rb"[A-Za-z][A-Za-z0-9_.-]{2,63}", data):
                text = token.decode("ascii", "replace")
                if text not in candidates and not any(char.isdigit() for char in text):
                    candidates.append(text)
            result["binary_plist_string_candidates"] = candidates[:100]
        else:
            text = data.decode("utf-8", "replace")
            result["xml_plist_keys"] = re.findall(r"<key>\s*([^<]+?)\s*</key>", text, flags=re.I)[:100]
    elif "x-dmap-tagged" in lowered:
        result["dmap"] = parse_dmap(data)
    elif "json" in lowered:
        try:
            result["json"] = summarize(json.loads(data.decode("utf-8", "replace")))
        except Exception as exc:
            result["json_error"] = type(exc).__name__
    return result


class Exporter:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def response(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host.lower()
        if not any(marker in host for marker in HOST_MARKERS):
            return
        split = urlsplit(flow.request.pretty_url)
        locale_headers = {
            key.lower(): value
            for key, value in flow.request.headers.items()
            if key.lower() in SAFE_VALUE_KEYS or "locale" in key.lower() or "language" in key.lower() or "store-front" in key.lower()
        }
        self.rows.append(
            {
                "method": flow.request.method,
                "host": host,
                "path": split.path,
                "query": {key: mask(key, value) for key, value in parse_qsl(split.query, keep_blank_values=True)},
                "locale_headers": locale_headers,
                "request": body_detail(flow.request.raw_content, flow.request.headers.get("content-type", "")),
                "response_status": flow.response.status_code,
                "response": body_detail(flow.response.raw_content, flow.response.headers.get("content-type", "")),
            }
        )

    def done(self) -> None:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(self.rows, ensure_ascii=False, indent=2), encoding="utf-8")


addons = [Exporter()]
