# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "cryptography>=46,<47",
# ]
# ///
"""Read-only scanner for duplicate album objects in Apple Music Library.musicdb.

The tool decrypts the AES/zlib envelope, parses track and album records, and
reports albums whose identical visible metadata is backed by multiple local
album persistent IDs. It never writes to the input database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import unicodedata
import zlib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


AES_KEY = b"BHUILuilfghuila3"


@dataclass(frozen=True)
class Section:
    tag: str
    offset: int
    length: int


@dataclass(frozen=True)
class Track:
    persistent_id: int
    title: str | None
    album: str | None
    artist: str | None
    album_artist: str | None
    composer: str | None
    compilation: int
    album_reference: int
    track_number: int


@dataclass(frozen=True)
class Album:
    persistent_id: int
    title: str | None
    album_artist: str | None
    artist: str | None
    offset: int
    length: int


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def decode_musicdb(path: Path) -> tuple[dict[str, Any], bytes]:
    file_data = path.read_bytes()
    if len(file_data) < 160 or file_data[:4] != b"hfma":
        raise ValueError("input is not an hfma Library.musicdb file")

    envelope_length = u32(file_data, 4)
    declared_file_size = u32(file_data, 8)
    max_crypt_size = u32(file_data, 84)
    if envelope_length < 88 or envelope_length >= len(file_data):
        raise ValueError(f"invalid envelope length: {envelope_length}")

    compressed = bytearray(file_data[envelope_length:])
    crypt_size = (
        max_crypt_size
        if 0 < max_crypt_size < len(compressed)
        else len(compressed) // 16 * 16
    )
    if crypt_size % 16:
        raise ValueError(f"AES region is not block aligned: {crypt_size}")

    decryptor = Cipher(algorithms.AES(AES_KEY), modes.ECB()).decryptor()
    compressed[:crypt_size] = decryptor.update(bytes(compressed[:crypt_size])) + decryptor.finalize()
    payload = zlib.decompress(compressed)

    header = {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(file_data).hexdigest(),
        "actual_file_size": len(file_data),
        "declared_file_size": declared_file_size,
        "envelope_length": envelope_length,
        "max_crypt_size": max_crypt_size,
        "decompressed_size": len(payload),
        "track_count_header": u32(file_data, 68),
        "playlist_count_header": u32(file_data, 72),
        "album_count_header": u32(file_data, 76),
        "artist_count_header": u32(file_data, 80),
    }
    return header, payload


def read_sections(payload: bytes) -> list[Section]:
    sections: list[Section] = []
    offset = 0
    while offset + 12 <= len(payload):
        try:
            tag = payload[offset : offset + 4].decode("ascii")
        except UnicodeDecodeError:
            break
        size_offset = offset + 8 if tag == "boma" else offset + 4
        length = u32(payload, size_offset)
        if length < 8 or offset + length > len(payload):
            break
        sections.append(Section(tag, offset, length))
        offset += length
    return sections


def boma_string(payload: bytes, section: Section) -> str | None:
    if section.length < 36:
        return None
    string_length = u32(payload, section.offset + 24)
    string_offset = section.offset + 36
    if not string_length or string_offset + string_length > section.offset + section.length:
        return None
    return payload[string_offset : string_offset + string_length].decode("utf-16-le", "replace").rstrip("\0")


def attached_bomas(sections: list[Section], index: int) -> Iterable[Section]:
    for section in sections[index + 1 :]:
        if section.tag != "boma":
            break
        yield section


def read_tracks(payload: bytes, sections: list[Section]) -> list[Track]:
    tracks: list[Track] = []
    for index, section in enumerate(sections):
        if section.tag != "itma" or section.length < 180:
            continue
        strings: dict[int, str] = {}
        for boma in attached_bomas(sections, index):
            subtype = u32(payload, boma.offset + 12)
            if subtype in {0x02, 0x03, 0x04, 0x0C, 0x1B}:
                value = boma_string(payload, boma)
                if value is not None:
                    strings[subtype] = value
        tracks.append(
            Track(
                persistent_id=u64(payload, section.offset + 16),
                title=strings.get(0x02),
                album=strings.get(0x03),
                artist=strings.get(0x04),
                album_artist=strings.get(0x1B),
                composer=strings.get(0x0C),
                compilation=payload[section.offset + 38],
                album_reference=u64(payload, section.offset + 172),
                track_number=struct.unpack_from("<H", payload, section.offset + 160)[0],
            )
        )
    return tracks


def read_albums(payload: bytes, sections: list[Section]) -> list[Album]:
    albums: list[Album] = []
    for index, section in enumerate(sections):
        if section.tag != "iama" or section.length < 24:
            continue
        strings: dict[int, str] = {}
        end = section.offset + section.length
        for boma in attached_bomas(sections, index):
            end = boma.offset + boma.length
            subtype = u32(payload, boma.offset + 12)
            if subtype in {0x12C, 0x12D, 0x12E}:
                value = boma_string(payload, boma)
                if value is not None:
                    strings[subtype] = value
        albums.append(
            Album(
                persistent_id=u64(payload, section.offset + 16),
                title=strings.get(0x12C),
                album_artist=strings.get(0x12D),
                artist=strings.get(0x12E),
                offset=section.offset,
                length=end - section.offset,
            )
        )
    return albums


def normalize(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def hex_id(value: int) -> str:
    return f"0x{value:016X}"


def scan_duplicates(header: dict[str, Any], tracks: list[Track], albums: list[Album]) -> dict[str, Any]:
    tracks_by_album: dict[int, list[Track]] = defaultdict(list)
    for track in tracks:
        tracks_by_album[track.album_reference].append(track)

    groups: dict[tuple[str, str, str], list[Album]] = defaultdict(list)
    for album in albums:
        key = (normalize(album.title), normalize(album.album_artist), normalize(album.artist))
        if key[0]:
            groups[key].append(album)

    candidates: list[dict[str, Any]] = []
    for group in groups.values():
        referenced = [album for album in group if tracks_by_album.get(album.persistent_id)]
        if len(referenced) < 2:
            continue

        variants: list[dict[str, Any]] = []
        all_tracks: list[Track] = []
        for album in sorted(referenced, key=lambda item: len(tracks_by_album[item.persistent_id]), reverse=True):
            album_tracks = sorted(
                tracks_by_album[album.persistent_id],
                key=lambda item: (item.track_number, item.title or ""),
            )
            all_tracks.extend(album_tracks)
            variants.append(
                {
                    "album_id": album.persistent_id,
                    "album_id_hex": hex_id(album.persistent_id),
                    "record_offset": album.offset,
                    "record_length": album.length,
                    "track_count": len(album_tracks),
                    "track_numbers": [track.track_number for track in album_tracks],
                    "tracks": [
                        {
                            "persistent_id": track.persistent_id,
                            "persistent_id_hex": hex_id(track.persistent_id),
                            "track_number": track.track_number,
                            "title": track.title,
                            "compilation": track.compilation,
                        }
                        for track in album_tracks
                    ],
                }
            )

        positive_numbers = [track.track_number for track in all_tracks if track.track_number > 0]
        overlapping_numbers = sorted(number for number, count in Counter(positive_numbers).items() if count > 1)
        counts = sorted((len(tracks_by_album[album.persistent_id]) for album in referenced), reverse=True)
        unique_majority = len(counts) == 1 or counts[0] > counts[1]
        exact_metadata = len({(album.title, album.album_artist, album.artist) for album in referenced}) == 1
        contiguous = bool(positive_numbers) and sorted(set(positive_numbers)) == list(range(1, max(positive_numbers) + 1))
        high_confidence = exact_metadata and unique_majority and not overlapping_numbers and contiguous

        survivor = variants[0] if unique_majority else None
        outliers = [] if survivor is None else [
            track
            for variant in variants[1:]
            for track in variant["tracks"]
        ]
        candidates.append(
            {
                "album": referenced[0].title,
                "album_artist": referenced[0].album_artist,
                "artist": referenced[0].artist,
                "classification": "likely_split_album" if high_confidence else "ambiguous_duplicate",
                "confidence": "high" if high_confidence else "review",
                "exact_visible_metadata": exact_metadata,
                "combined_track_count": len(all_tracks),
                "contiguous_track_numbers": contiguous,
                "overlapping_track_numbers": overlapping_numbers,
                "suggested_survivor_album_id": survivor["album_id"] if survivor else None,
                "suggested_survivor_album_id_hex": survivor["album_id_hex"] if survivor else None,
                "outlier_tracks": outliers,
                "variants": variants,
            }
        )

    candidates.sort(
        key=lambda item: (
            item["classification"] != "likely_split_album",
            normalize(item["album_artist"]),
            normalize(item["album"]),
        )
    )
    likely = [item for item in candidates if item["classification"] == "likely_split_album"]
    return {
        "mode": "read_only_duplicate_album_scan",
        "database": header,
        "parsed_track_count": len(tracks),
        "parsed_album_count": len(albums),
        "duplicate_album_groups": len(candidates),
        "likely_split_album_groups": len(likely),
        "writes_generated": 0,
        "candidates": candidates,
    }


def markdown_report(result: dict[str, Any]) -> str:
    db = result["database"]
    lines = [
        "# Apple Music duplicate album scan",
        "",
        f"- Database: `{db['path']}`",
        f"- SHA-256: `{db['sha256']}`",
        f"- Parsed tracks: {result['parsed_track_count']:,}",
        f"- Parsed album objects: {result['parsed_album_count']:,}",
        f"- Duplicate groups requiring review: {result['duplicate_album_groups']:,}",
        f"- High-confidence split albums: {result['likely_split_album_groups']:,}",
        "- Writes generated: 0",
        "",
    ]
    for candidate in result["candidates"]:
        lines.extend(
            [
                f"## {candidate['album']} — {candidate['album_artist'] or candidate['artist'] or '(unknown artist)'}",
                "",
                f"- Classification: `{candidate['classification']}`",
                f"- Combined tracks: {candidate['combined_track_count']}",
                f"- Suggested survivor: `{candidate['suggested_survivor_album_id_hex'] or 'manual review'}`",
            ]
        )
        if candidate["overlapping_track_numbers"]:
            lines.append(f"- Overlapping track numbers: {candidate['overlapping_track_numbers']}")
        lines.append("")
        for variant in candidate["variants"]:
            numbers = ", ".join(str(number) for number in variant["track_numbers"])
            lines.append(
                f"- `{variant['album_id_hex']}`: {variant['track_count']} tracks; numbers {numbers}"
            )
        if candidate["outlier_tracks"]:
            lines.extend(["", "Proposed track reference changes:", ""])
            for track in candidate["outlier_tracks"]:
                lines.append(
                    f"- Track {track['track_number']} `{track['title']}` "
                    f"(`{track['persistent_id_hex']}`) → `{candidate['suggested_survivor_album_id_hex']}`"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Path to a clean Library.musicdb copy")
    parser.add_argument("--json", type=Path, dest="json_output", help="Write the full machine-readable report")
    parser.add_argument("--markdown", type=Path, dest="markdown_output", help="Write a human-readable report")
    parser.add_argument("--only-likely", action="store_true", help="Omit ambiguous duplicate groups from report output")
    args = parser.parse_args()

    header, payload = decode_musicdb(args.database)
    sections = read_sections(payload)
    if not sections:
        raise ValueError("no database sections could be parsed")
    result = scan_duplicates(header, read_tracks(payload, sections), read_albums(payload, sections))
    if args.only_likely:
        result["candidates"] = [
            item for item in result["candidates"] if item["classification"] == "likely_split_album"
        ]

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown_report(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
