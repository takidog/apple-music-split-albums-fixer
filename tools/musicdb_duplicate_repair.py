# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "cryptography>=46,<47",
# ]
# ///
"""Repair high-confidence duplicate album objects in Apple Music Library.musicdb.

The default mode is a dry run. Writing requires --apply and --output. The input
file is never modified in place. Use only a clean-shutdown database copy.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from musicdb_duplicate_scanner import (
    AES_KEY,
    decode_musicdb,
    read_albums,
    read_sections,
    read_tracks,
    scan_duplicates,
    u32,
    u64,
)


APPLE_EPOCH_OFFSET = 2_082_844_800


def put_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def find_track_sections(payload: bytes, sections: list[Any]) -> dict[int, Any]:
    return {
        u64(payload, section.offset + 16): section
        for section in sections
        if section.tag == "itma" and section.length >= 180
    }


def find_numeric_boma(payload: bytes, sections: list[Any], track_section: Any) -> Any | None:
    started = False
    for section in sections:
        if section is track_section:
            started = True
            continue
        if not started:
            continue
        if section.tag != "boma":
            return None
        if section.length >= 43 and u32(payload, section.offset + 12) == 1:
            return section
    return None


def plan_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Convert one high-confidence scanner result into a repair operation."""
    if candidate["classification"] != "likely_split_album":
        raise ValueError("only high-confidence split albums can be repaired")
    survivor = candidate["suggested_survivor_album_id"]
    remove_ids = [
        variant["album_id"]
        for variant in candidate["variants"]
        if variant["album_id"] != survivor
    ]
    return {
        "album": candidate["album"],
        "album_artist": candidate["album_artist"],
        "artist": candidate["artist"],
        "survivor_album_id": survivor,
        "survivor_album_id_hex": candidate["suggested_survivor_album_id_hex"],
        "remove_album_ids": remove_ids,
        "remove_album_ids_hex": [f"0x{value:016X}" for value in remove_ids],
        "tracks_to_reassign": candidate["outlier_tracks"],
    }


def build_plan(scan: dict[str, Any], selected_albums: set[str] | None) -> list[dict[str, Any]]:
    return [
        plan_candidate(candidate)
        for candidate in scan["candidates"]
        if candidate["classification"] == "likely_split_album"
        and (not selected_albums or candidate["album"] in selected_albums)
    ]


def repair_payload(payload: bytes, plan: list[dict[str, Any]]) -> tuple[bytes, dict[str, Any]]:
    sections = read_sections(payload)
    albums = {album.persistent_id: album for album in read_albums(payload, sections)}
    track_sections = find_track_sections(payload, sections)
    output = bytearray(payload)
    changed_tracks: list[dict[str, Any]] = []
    removed_records: list[dict[str, Any]] = []

    for item in plan:
        survivor = item["survivor_album_id"]
        for track in item["tracks_to_reassign"]:
            track_id = track["persistent_id"]
            section = track_sections.get(track_id)
            if section is None:
                raise ValueError(f"track section not found: 0x{track_id:016X}")
            old_reference = u64(output, section.offset + 172)
            if old_reference == survivor:
                continue
            output[section.offset + 172 : section.offset + 180] = survivor.to_bytes(8, "little")

            # In the known Apple Music transaction, these two bytes changed
            # from 0/0 to 1/1 only on the item sent to Cloud Library.
            numeric = find_numeric_boma(payload, sections, section)
            sync_marker_changed = False
            if numeric is not None:
                output[numeric.offset + 41] = 1
                output[numeric.offset + 42] = 1
                sync_marker_changed = True
            changed_tracks.append(
                {
                    "album": item["album"],
                    "album_artist": item.get("album_artist"),
                    "artist": item.get("artist"),
                    "persistent_id": track_id,
                    "persistent_id_hex": f"0x{track_id:016X}",
                    "title": track.get("title"),
                    "track_number": track.get("track_number"),
                    "old_album_id_hex": f"0x{old_reference:016X}",
                    "new_album_id_hex": f"0x{survivor:016X}",
                    "sync_marker_changed": sync_marker_changed,
                }
            )

        for album_id in item["remove_album_ids"]:
            album = albums.get(album_id)
            if album is None:
                raise ValueError(f"album record not found: 0x{album_id:016X}")
            removed_records.append(
                {
                    "album": item["album"],
                    "persistent_id": album_id,
                    "persistent_id_hex": f"0x{album_id:016X}",
                    "offset": album.offset,
                    "length": album.length,
                }
            )

    if not changed_tracks and not removed_records:
        return payload, {"changed_tracks": [], "removed_album_records": []}

    lama_index = next((index for index, section in enumerate(sections) if section.tag == "lama"), None)
    if lama_index is None or lama_index == 0:
        raise ValueError("lama album master section not found")
    lama = sections[lama_index]
    album_hsma = sections[lama_index - 1]
    if album_hsma.tag != "hsma":
        raise ValueError("album hsma section not found before lama")
    internal_hfma = next((section for section in sections if section.tag == "hfma" and section.length >= 128), None)
    if internal_hfma is None:
        raise ValueError("internal hfma header not found")

    # Apple Music 1.6 and earlier store the hsma span at +8; 1.7 stores zero
    # there and moves the span to +16. Use whichever matches the real span.
    album_hsma_end = next(
        (section.offset for section in sections[lama_index:] if section.tag == "hsma"),
        len(payload),
    )
    album_hsma_span = album_hsma_end - album_hsma.offset
    span_offset = next(
        (offset for offset in (8, 16) if u32(output, album_hsma.offset + offset) == album_hsma_span),
        None,
    )
    if span_offset is None:
        raise ValueError("album hsma section length field not found")

    removed_count = len(removed_records)
    removed_length = sum(record["length"] for record in removed_records)
    lama_count = u32(output, lama.offset + 8)
    if lama_count < removed_count:
        raise ValueError("album count underflow")
    put_u32(output, lama.offset + 8, lama_count - removed_count)
    put_u32(output, album_hsma.offset + span_offset, album_hsma_span - removed_length)
    put_u32(output, internal_hfma.offset + 100, int(time.time()) + APPLE_EPOCH_OFFSET)

    for record in sorted(removed_records, key=lambda value: value["offset"], reverse=True):
        start = record["offset"]
        del output[start : start + record["length"]]

    return bytes(output), {
        "changed_tracks": changed_tracks,
        "removed_album_records": removed_records,
        "removed_album_count": removed_count,
        "removed_payload_bytes": removed_length,
    }


def encode_musicdb(source: Path, payload: bytes) -> bytes:
    original = source.read_bytes()
    envelope_length = u32(original, 4)
    max_crypt_size = u32(original, 84)
    header = bytearray(original[:envelope_length])
    # Apple Music 1.6.4 uses zlib level 1; the known after-state reproduces
    # its compressed payload size exactly with this setting.
    compressed = bytearray(zlib.compress(payload, level=1))
    crypt_size = (
        max_crypt_size
        if 0 < max_crypt_size < len(compressed)
        else len(compressed) // 16 * 16
    )
    encryptor = Cipher(algorithms.AES(AES_KEY), modes.ECB()).encryptor()
    compressed[:crypt_size] = encryptor.update(bytes(compressed[:crypt_size])) + encryptor.finalize()
    put_u32(header, 8, len(header) + len(compressed))
    # Apple Music 1.7 repeats the file size at +128; older versions leave it zero.
    if len(header) >= 132 and u32(original, 128) == len(original):
        put_u32(header, 128, len(header) + len(compressed))
    payload_sections = read_sections(payload)
    internal_hfma = next(section for section in payload_sections if section.tag == "hfma" and section.length >= 128)
    lama = next(section for section in payload_sections if section.tag == "lama")
    put_u32(header, 76, u32(payload, lama.offset + 8))
    put_u32(header, 100, u32(payload, internal_hfma.offset + 100))
    return bytes(header + compressed)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--album", action="append", help="Repair only an exact album title; repeatable")
    parser.add_argument("--apply", action="store_true", help="Enable creation of a repaired database")
    parser.add_argument("--output", type=Path, help="Required with --apply; input is never overwritten")
    parser.add_argument("--report", type=Path, help="Write transaction plan/result JSON")
    args = parser.parse_args()
    if args.apply and args.output is None:
        parser.error("--output is required with --apply")
    if args.output and not args.apply:
        parser.error("--output requires --apply")
    if args.output and args.output.resolve() == args.database.resolve():
        parser.error("input and output must be different paths")

    header, payload = decode_musicdb(args.database)
    sections = read_sections(payload)
    scan = scan_duplicates(header, read_tracks(payload, sections), read_albums(payload, sections))
    selected = set(args.album) if args.album else None
    plan = build_plan(scan, selected)
    repaired_payload, changes = repair_payload(payload, plan)
    result: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry_run",
        "source": header,
        "selected_album_filter": sorted(selected) if selected else None,
        "planned_album_repairs": len(plan),
        "plan": plan,
        **changes,
        "output": None,
        "validation": None,
    }

    if args.apply:
        encoded = encode_musicdb(args.database, repaired_payload)
        atomic_write(args.output, encoded)
        verify_header, verify_payload = decode_musicdb(args.output)
        verify_sections = read_sections(verify_payload)
        verify_scan = scan_duplicates(
            verify_header,
            read_tracks(verify_payload, verify_sections),
            read_albums(verify_payload, verify_sections),
        )
        repaired_names = {item["album"] for item in plan}
        remaining = [
            item["album"]
            for item in verify_scan["candidates"]
            if item["classification"] == "likely_split_album" and item["album"] in repaired_names
        ]
        if remaining:
            args.output.unlink(missing_ok=True)
            raise ValueError(f"validation failed; repaired albums still split: {remaining}")
        result["output"] = {
            "path": str(args.output.resolve()),
            "size": len(encoded),
            "sha256": verify_header["sha256"],
        }
        result["validation"] = {
            "decoded": True,
            "parsed_track_count": len(read_tracks(verify_payload, verify_sections)),
            "parsed_album_count": len(read_albums(verify_payload, verify_sections)),
            "repaired_albums_remaining_split": remaining,
        }

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
