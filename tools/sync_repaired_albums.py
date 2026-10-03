# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "cryptography>=46,<47",
#   "mitmproxy==12.2.3",
#   "requests>=2.32,<3",
# ]
# ///
"""Build and send signed Cloud Library /edit requests.

Default mode is a dry run. --apply attempts a two-phase compilation-field touch
for the locally reassigned tracks: first the opposite value, then the album's
consensus value. Apple requires a request-specific X-Apple-ActionSignature, so
--apply also requires a compatible Windows SAP signer. Sensitive values are
never written to reports.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import struct
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import requests
from mitmproxy import http, io

from musicdb_duplicate_scanner import (
    decode_musicdb,
    read_albums,
    read_sections,
    read_tracks,
    scan_duplicates,
    u64,
)


def dmap(tag: str, payload: bytes) -> bytes:
    return tag.encode("ascii") + struct.pack(">I", len(payload)) + payload


def dmap_int(tag: str, value: int, length: int) -> bytes:
    return dmap(tag, value.to_bytes(length, "big"))


def parse_dmap(data: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while offset + 8 <= len(data):
        tag = data[offset : offset + 4].decode("ascii", "replace")
        length = int.from_bytes(data[offset + 4 : offset + 8], "big")
        end = offset + 8 + length
        if end > len(data):
            break
        payload = data[offset + 8 : end]
        row: dict[str, Any] = {"tag": tag, "length": length}
        if tag in {"mebp", "mebs", "mlcl", "mlit", "mupd", "adbs"}:
            row["children"] = parse_dmap(payload)
        elif length in {1, 2, 4, 8}:
            row["integer"] = int.from_bytes(payload, "big")
        rows.append(row)
        offset = end
    return rows


def find_values(nodes: Iterable[dict[str, Any]], tag: str) -> list[int]:
    values: list[int] = []
    for node in nodes:
        if node.get("tag") == tag and "integer" in node:
            values.append(node["integer"])
        values.extend(find_values(node.get("children", []), tag))
    return values


def latest_library_context(capture: Path) -> tuple[http.HTTPFlow, int]:
    candidates: list[http.HTTPFlow] = []
    with capture.open("rb") as handle:
        for flow in io.FlowReader(handle).stream():
            if not isinstance(flow, http.HTTPFlow) or flow.response is None:
                continue
            if flow.request.pretty_host.lower() != "librarydaap.itunes.apple.com":
                continue
            if flow.response.status_code != 200:
                continue
            candidates.append(flow)
    if not candidates:
        raise ValueError("no completed authenticated librarydaap flow found")

    revision_flows = [
        flow for flow in candidates
        if flow.request.path.split("?", 1)[0].endswith("/daap/update")
        and "x-dmap-tagged" in flow.response.headers.get("content-type", "").lower()
    ]
    if not revision_flows:
        raise ValueError("no completed Cloud Library revision response found")
    revision_flow = revision_flows[-1]
    revisions = find_values(parse_dmap(revision_flow.response.raw_content or b""), "musr")
    if not revisions:
        raise ValueError("Cloud Library revision tag musr was not found")
    return revision_flow, revisions[-1]


def cloud_ids(
    database: Path,
    transaction: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], int]]:
    _, payload = decode_musicdb(database)
    sections = read_sections(payload)
    tracks = read_tracks(payload, sections)
    albums = read_albums(payload, sections)
    scan = scan_duplicates({}, tracks, albums)
    track_sections = {
        u64(payload, section.offset + 16): section
        for section in sections
        if section.tag == "itma" and section.length >= 228
    }
    desired_by_album: dict[tuple[str, str], int] = {}
    repair_keys_by_title: dict[str, list[tuple[str, str]]] = {}
    for repair in transaction["plan"]:
        matches = [
            item
            for item in scan["candidates"]
            if item["album"] == repair["album"]
            and (
                repair.get("album_artist") is None
                or item.get("album_artist") == repair.get("album_artist")
            )
            and (repair.get("artist") is None or item.get("artist") == repair.get("artist"))
        ]
        if len(matches) != 1:
            raise ValueError(f"album is missing from source scan: {repair['album']}")
        candidate = matches[0]
        values = [
            track["compilation"]
            for variant in candidate["variants"]
            for track in variant["tracks"]
        ]
        counts = Counter(values)
        desired, count = counts.most_common(1)[0]
        if sum(1 for value in counts.values() if value == count) > 1:
            raise ValueError(f"compilation value is tied for album: {repair['album']}")
        key = (repair["album"], repair.get("album_artist") or "")
        desired_by_album[key] = desired
        repair_keys_by_title.setdefault(repair["album"], []).append(key)

    items: list[dict[str, Any]] = []
    for changed in transaction["changed_tracks"]:
        if changed.get("album_artist") is not None:
            repair_key = (changed["album"], changed.get("album_artist") or "")
        else:
            title_keys = repair_keys_by_title.get(changed["album"], [])
            if len(title_keys) != 1:
                raise ValueError(
                    f"album artist is required to disambiguate: {changed['album']}"
                )
            repair_key = title_keys[0]
        track_id = changed["persistent_id"]
        section = track_sections.get(track_id)
        if section is None:
            raise ValueError(f"track not found in source database: 0x{track_id:016X}")
        cloud_id = u64(payload, section.offset + 220)
        if not 0 < cloud_id <= 0xFFFFFFFF:
            raise ValueError(f"invalid Cloud Library miid {cloud_id} for track 0x{track_id:016X}")
        items.append(
            {
                "album": changed["album"],
                "album_artist": repair_key[1],
                "title": changed.get("title"),
                "local_persistent_id": track_id,
                "cloud_miid": cloud_id,
                "desired_asco": desired_by_album[repair_key],
            }
        )
    if len({item["cloud_miid"] for item in items}) != len(items):
        raise ValueError("duplicate Cloud Library miid values in repair set")
    return items, desired_by_album


def edit_body(revision: int, values: list[tuple[int, int]]) -> bytes:
    item_list = b"".join(
        dmap("mlit", dmap_int("miid", miid, 4) + dmap_int("asco", asco, 1))
        for miid, asco in values
    )
    root = (
        dmap_int("mstc", int(time.time()), 4)
        + dmap_int("mlid", 0, 4)
        + dmap_int("musr", revision, 4)
        + dmap_int("mikd", 2, 1)
        + dmap("mlcl", item_list)
    )
    return dmap("mebs", root)


def request_context(flow: http.HTTPFlow) -> tuple[str, dict[str, str]]:
    split = urlsplit(flow.request.pretty_url)
    base_path = split.path.split("/daap/", 1)[0]
    url = urlunsplit((split.scheme, split.netloc, base_path + "/daap/databases/1/edit", "", ""))
    excluded = {
        "host", "content-length", "content-type", "accept-encoding", "connection",
        "if-none-match", "if-match", "client-cloud-daap-request-reason",
        "x-daap-client-features-versions", "x-apple-actionsignature",
    }
    headers = {
        name: value
        for name, value in flow.request.headers.items()
        if name.lower() not in excluded and not name.startswith(":")
    }
    headers["Content-Type"] = "application/x-dmap-tagged"
    headers["Accept-Encoding"] = "identity"
    return url, headers


def resolve_sap_signer(configured: Path | None) -> Path:
    candidates: list[Path] = []
    project_root = Path(__file__).resolve().parent.parent
    if configured is not None:
        candidates.append(configured)
    if value := os.environ.get("APPLE_MUSIC_SAP_SIGNER"):
        candidates.append(Path(value))
    candidates.extend(
        [
            Path(__file__).resolve().parent / "sapsigner.exe",
            project_root / "sapsigner.exe",
            project_root / "vendor" / "ipatool-webGUI"
            / "tools" / "sapsigner.exe",
            project_root / "work" / "vendor" / "ipatool-webGUI"
            / "tools" / "sapsigner.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Signum" / "resources" / "apple-tools" / "windows-x64"
            / "v3-legacy" / "sapsigner.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Signum" / "resources" / "apple-tools" / "windows-x64"
            / "v2" / "sapsigner.exe",
        ]
    )
    for candidate in candidates:
        candidate = candidate.expanduser().resolve()
        if candidate.is_file():
            return candidate
    raise ValueError(
        "sapsigner.exe was not found; pass --sap-signer or set APPLE_MUSIC_SAP_SIGNER"
    )


def sign_body(signer: Path, body: bytes, timeout: int) -> bytes:
    result = subprocess.run(
        [str(signer)],
        input=body,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=signer.parent,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"SAP signer failed with exit {result.returncode}: {message}")
    if not result.stdout:
        raise RuntimeError("SAP signer returned an empty signature")
    return result.stdout


def refresh_revision(
    session: requests.Session,
    flow: http.HTTPFlow,
    signer: Path,
    timeout: int,
) -> tuple[int, dict[str, Any]]:
    body = flow.request.raw_content or b""
    excluded = {"host", "content-length", "accept-encoding", "connection", "x-apple-actionsignature"}
    headers = {
        name: value
        for name, value in flow.request.headers.items()
        if name.lower() not in excluded and not name.startswith(":")
    }
    signature = sign_body(signer, body, timeout)
    headers["X-Apple-ActionSignature"] = base64.b64encode(signature).decode("ascii")
    headers["Accept-Encoding"] = "identity"
    response = session.request(
        flow.request.method,
        flow.request.pretty_url,
        headers=headers,
        data=body,
        timeout=timeout,
    )
    parsed = parse_dmap(response.content)
    statuses = find_values(parsed, "mstt")
    revisions = find_values(parsed, "musr")
    if response.status_code != 200 or not revisions:
        raise RuntimeError(
            f"Cloud Library revision refresh failed: HTTP {response.status_code}, "
            f"statuses={statuses}, revisions={revisions}"
        )
    return revisions[-1], {
        "http_status": response.status_code,
        "response_length": len(response.content),
        "server_statuses": statuses,
        "revision": revisions[-1],
        "signature_length": len(signature),
    }


def verify_cloud_items(
    session: requests.Session,
    flow: http.HTTPFlow,
    signer: Path,
    revision: int,
    delta: int,
    expected: dict[int, int],
    timeout: int,
) -> dict[str, Any]:
    split = urlsplit(flow.request.pretty_url)
    base_path = split.path.split("/daap/", 1)[0]
    url = urlunsplit((split.scheme, split.netloc, base_path + "/daap/databases/1/items", "", ""))
    body = (
        f"session-id=0&revision-number={revision}&delta={delta}&type=music&meta=all"
    ).encode("ascii")
    excluded = {"host", "content-length", "content-type", "accept-encoding", "connection", "x-apple-actionsignature"}
    headers = {
        name: value
        for name, value in flow.request.headers.items()
        if name.lower() not in excluded and not name.startswith(":")
    }
    signature = sign_body(signer, body, timeout)
    headers["X-Apple-ActionSignature"] = base64.b64encode(signature).decode("ascii")
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers["Accept-Encoding"] = "identity"
    response = session.post(url, headers=headers, data=body, timeout=timeout)
    parsed = parse_dmap(response.content)
    returned: dict[int, int] = {}

    def walk(nodes: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        for node in nodes:
            yield node
            yield from walk(node.get("children", []))

    for node in walk(parsed):
        if node.get("tag") != "mlit":
            continue
        cloud_ids = find_values(node.get("children", []), "miid")
        compilation_values = find_values(node.get("children", []), "asco")
        if cloud_ids and compilation_values:
            returned[cloud_ids[0]] = compilation_values[-1]

    matched = {cloud_id: returned[cloud_id] for cloud_id in expected if cloud_id in returned}
    mismatches = {
        str(cloud_id): {"expected": expected[cloud_id], "actual": actual}
        for cloud_id, actual in matched.items()
        if expected[cloud_id] != actual
    }
    missing = sorted(set(expected) - set(matched))
    return {
        "http_status": response.status_code,
        "response_length": len(response.content),
        "revision": revision,
        "delta": delta,
        "returned_item_count": len(returned),
        "expected_item_count": len(expected),
        "matched_item_count": len(matched),
        "missing_cloud_miids": missing,
        "mismatches": mismatches,
        "signature_length": len(signature),
        "verified": response.status_code == 200 and not missing and not mismatches,
    }


def send_phase(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    revision: int,
    values: list[tuple[int, int]],
    signer: Path,
    timeout: int,
) -> tuple[int, dict[str, Any]]:
    body = edit_body(revision, values)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            signed_headers = dict(headers)
            signature = sign_body(signer, body, timeout)
            signed_headers["X-Apple-ActionSignature"] = base64.b64encode(signature).decode("ascii")
            response = session.post(url, headers=signed_headers, data=body, timeout=timeout)
            parsed = parse_dmap(response.content)
            statuses = find_values(parsed, "mstt")
            revisions = find_values(parsed, "musr")
            if response.status_code == 200 and statuses and statuses[0] == 200 and revisions:
                return revisions[0], {
                    "http_status": response.status_code,
                    "response_length": len(response.content),
                    "server_statuses": statuses,
                    "revision_before": revision,
                    "revision_after": revisions[0],
                    "attempt": attempt,
                    "signature_length": len(signature),
                }
            raise RuntimeError(
                f"edit rejected: HTTP {response.status_code}, statuses={statuses}, revisions={revisions}"
            )
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(attempt)
    raise RuntimeError(f"Cloud Library edit failed after retries: {last_error}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Pre-repair clean database used to map local IDs")
    parser.add_argument("transaction", type=Path, help="JSON report from musicdb_duplicate_repair.py")
    parser.add_argument("capture", type=Path, help="Recent mitmproxy capture containing authenticated /update")
    parser.add_argument("--album", action="append", help="Sync only an exact album title; repeatable")
    parser.add_argument(
        "--exclude-album",
        action="append",
        help="Exclude an exact album title; repeatable",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--proxy", default="direct", help="Proxy URL, or 'direct' for direct HTTPS")
    parser.add_argument("--ca", type=Path, default=Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--sap-signer",
        type=Path,
        help="Path to a Windows sapsigner.exe compatible with Apple SAP signing",
    )
    args = parser.parse_args()

    transaction = json.loads(args.transaction.read_text(encoding="utf-8"))
    items, desired = cloud_ids(args.database, transaction)
    if args.album:
        selected = set(args.album)
        items = [item for item in items if item["album"] in selected]
        desired = {key: value for key, value in desired.items() if key[0] in selected}
        if not items:
            raise ValueError("album filter matched no repaired items")
    if args.exclude_album:
        excluded_albums = set(args.exclude_album)
        items = [item for item in items if item["album"] not in excluded_albums]
        desired = {
            key: value for key, value in desired.items() if key[0] not in excluded_albums
        }
        if not items:
            raise ValueError("album exclusion removed every repaired item")
    flow, revision = latest_library_context(args.capture)
    url, headers = request_context(flow)
    report: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry_run",
        "item_count": len(items),
        "album_count": len(desired),
        "captured_revision": revision,
        "initial_revision": revision,
        "revision_refresh": None,
        "items": [
            {
                "album": item["album"],
                "album_artist": item["album_artist"],
                "title": item["title"],
                "local_persistent_id_hex": f"0x{item['local_persistent_id']:016X}",
                "cloud_miid": item["cloud_miid"],
                "desired_asco": item["desired_asco"],
            }
            for item in items
        ],
        "phase_one": None,
        "phase_two": None,
        "verification_revision_refresh": None,
        "verification": None,
        "final_revision": None,
    }

    if args.apply:
        signer = resolve_sap_signer(args.sap_signer)
        session = requests.Session()
        if args.proxy.lower() == "direct":
            session.trust_env = False
            session.verify = True
        else:
            if not args.ca.exists():
                raise ValueError(f"mitmproxy CA file not found: {args.ca}")
            session.proxies.update({"http": args.proxy, "https": args.proxy})
            session.verify = str(args.ca)
        revision, report["revision_refresh"] = refresh_revision(
            session, flow, signer, args.timeout
        )
        report["initial_revision"] = revision
        phase_one = [(item["cloud_miid"], 0 if item["desired_asco"] else 1) for item in items]
        revision, report["phase_one"] = send_phase(
            session, url, headers, revision, phase_one, signer, args.timeout
        )
        phase_two = [(item["cloud_miid"], item["desired_asco"]) for item in items]
        revision, report["phase_two"] = send_phase(
            session, url, headers, revision, phase_two, signer, args.timeout
        )
        report["final_revision"] = revision
        verification_revision, report["verification_revision_refresh"] = refresh_revision(
            session, flow, signer, args.timeout
        )
        expected = {item["cloud_miid"]: item["desired_asco"] for item in items}
        report["verification"] = verify_cloud_items(
            session,
            flow,
            signer,
            verification_revision,
            report["initial_revision"],
            expected,
            args.timeout,
        )

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if args.apply and not report["verification"]["verified"]:
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
