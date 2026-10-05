# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "cryptography>=46,<47",
#   "mitmproxy==12.2.3",
#   "requests>=2.32,<3",
# ]
# ///
"""Interactive Apple Music split-album repair and Cloud Library sync wizard.

The database parser, repair planner, and cloud protocol are platform-neutral.
Windows-specific discovery and Music process control are isolated in this file
so a future macOS adapter can reuse the same repair workflow.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from musicdb_duplicate_repair import (
    atomic_write,
    encode_musicdb,
    plan_candidate,
    repair_payload,
)
from musicdb_duplicate_scanner import (
    decode_musicdb,
    normalize,
    read_albums,
    read_sections,
    read_tracks,
    scan_duplicates,
)
from sync_repaired_albums import (
    cloud_ids,
    latest_library_context,
    refresh_revision,
    request_context,
    resolve_sap_signer,
    send_phase,
    verify_cloud_items,
)


MUSIC_PROCESS_NAMES = {"applemusic.exe", "amplibraryagent.exe"}
MACOS_MUSIC_PROCESS_NAMES = ["Music", "AMPLibraryAgent"]


def default_database() -> Path:
    if sys.platform == "win32":
        root = Path.home() / "Music" / "Apple Music"
        preferred = root / "Apple Music Library.musiclibrary" / "Library.musicdb"
    elif sys.platform == "darwin":
        root = Path.home() / "Music" / "Music"
        preferred = root / "Music Library.musiclibrary" / "Library.musicdb"
    else:
        raise ValueError("automatic database discovery is available on Windows and macOS only")
    if preferred.is_file():
        return preferred
    candidates = list(root.glob("*.musiclibrary/Library.musicdb")) if root.is_dir() else []
    if not candidates:
        raise ValueError("Library.musicdb was not found; pass --database")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def newest_capture() -> Path | None:
    roots = [Path.cwd() / "captures", Path(__file__).resolve().parent.parent / "captures"]
    candidates = {
        path.resolve()
        for root in roots
        if root.is_dir()
        for path in root.glob("*.mitm")
        if path.is_file()
    }
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def scan_database(database: Path) -> tuple[dict[str, Any], bytes]:
    header, payload = decode_musicdb(database)
    sections = read_sections(payload)
    result = scan_duplicates(
        header,
        read_tracks(payload, sections),
        read_albums(payload, sections),
    )
    return result, payload


def candidate_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    return (
        normalize(candidate.get("album")),
        normalize(candidate.get("album_artist")),
        normalize(candidate.get("artist")),
    )


def safe_candidates(scan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        candidate
        for candidate in scan["candidates"]
        if candidate["classification"] == "likely_split_album"
    ]


def display_candidates(scan: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = safe_candidates(scan)
    print(f"\nScanned {scan['parsed_track_count']:,} tracks and {scan['parsed_album_count']:,} albums.")
    if not candidates:
        print("No high-confidence split albums were found.")
    else:
        print(f"Found {len(candidates)} high-confidence split album(s):\n")
        for index, candidate in enumerate(candidates, 1):
            artist = candidate.get("album_artist") or candidate.get("artist") or "Unknown artist"
            print(
                f"[{index:>2}] {candidate['album']} — {artist} "
                f"| {len(candidate['variants'])} parts, "
                f"{candidate['combined_track_count']} tracks, "
                f"move {len(candidate['outlier_tracks'])}"
            )
    ambiguous = sum(
        candidate["classification"] == "ambiguous_duplicate"
        for candidate in scan["candidates"]
    )
    if ambiguous:
        print(f"\n{ambiguous} ambiguous group(s) were left out because automatic repair is unsafe.")
    return candidates


def parse_selection(value: str, count: int) -> list[int]:
    text = value.strip().lower()
    if text in {"all", "a"}:
        return list(range(count))
    selected: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"invalid descending range: {part}")
            values = range(start, end + 1)
        else:
            values = [int(part)]
        for number in values:
            if not 1 <= number <= count:
                raise ValueError(f"selection is outside 1-{count}: {number}")
            selected.add(number - 1)
    if not selected:
        raise ValueError("no albums were selected")
    return sorted(selected)


def running_music_processes() -> list[tuple[int, str]]:
    if sys.platform == "darwin":
        rows: list[tuple[int, str]] = []
        for name in MACOS_MUSIC_PROCESS_NAMES:
            result = subprocess.run(
                ["pgrep", "-x", "-U", str(os.getuid()), name],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            rows.extend((int(pid), name) for pid in result.stdout.split())
        return rows
    if sys.platform != "win32":
        return []
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    rows: list[tuple[int, str]] = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) < 2 or row[0].casefold() not in MUSIC_PROCESS_NAMES:
            continue
        try:
            rows.append((int(row[1]), row[0]))
        except ValueError:
            continue
    return rows


def stop_music_processes(processes: list[tuple[int, str]]) -> None:
    if sys.platform == "darwin":
        # Quit Music normally so it flushes the library, then stop the agent.
        if any(name == "Music" for _, name in processes):
            subprocess.run(
                ["osascript", "-e", 'quit app "Music"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            for _ in range(40):
                if not any(name == "Music" for _, name in running_music_processes()):
                    break
                time.sleep(0.25)
        for process_id, name in running_music_processes():
            if name != "Music":
                subprocess.run(["kill", "-TERM", str(process_id)], check=False)
        for _ in range(20):
            if not running_music_processes():
                return
            time.sleep(0.25)
        names = ", ".join(name for _, name in running_music_processes())
        raise RuntimeError(f"Apple Music processes are still running: {names}")
    for process_id, name in processes:
        result = subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode and any(pid == process_id for pid, _ in running_music_processes()):
            raise RuntimeError(f"could not stop {name} (PID {process_id})")
    for _ in range(20):
        if not running_music_processes():
            return
        time.sleep(0.25)
    names = ", ".join(name for _, name in running_music_processes())
    raise RuntimeError(f"Apple Music processes are still running: {names}")


def start_apple_music() -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Music"])
        return
    if sys.platform != "win32":
        return
    subprocess.Popen(
        ["explorer.exe", "shell:AppsFolder\\AppleInc.AppleMusicWin_nzyj5cx40ttqa!App"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def library_root(database: Path) -> Path | None:
    for parent in [database.parent, *database.parents]:
        if parent.name.casefold().endswith(".musiclibrary"):
            return parent
    return None


def create_backup(database: Path, run_directory: Path) -> Path:
    root = library_root(database)
    backup_directory = run_directory / "backup"
    if root is None:
        backup_directory.mkdir(parents=True)
        target = backup_directory / database.name
        shutil.copy2(database, target)
        return target
    destination = backup_directory / root.name
    shutil.copytree(root, destination, copy_function=shutil.copy2)
    return destination / database.relative_to(root)


def validate_output(database: Path, selected_keys: set[tuple[str, str, str]]) -> dict[str, Any]:
    scan, _ = scan_database(database)
    remaining = [
        candidate["album"]
        for candidate in safe_candidates(scan)
        if candidate_key(candidate) in selected_keys
    ]
    if remaining:
        raise ValueError(f"repaired albums are still split: {remaining}")
    return {
        "decoded": True,
        "parsed_track_count": scan["parsed_track_count"],
        "parsed_album_count": scan["parsed_album_count"],
        "repaired_albums_remaining_split": remaining,
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def synchronize_cloud(
    database: Path,
    transaction: dict[str, Any],
    capture: Path,
    signer: Path,
    proxy: str,
    ca: Path,
    timeout: int,
) -> dict[str, Any]:
    items, desired = cloud_ids(database, transaction)
    flow, captured_revision = latest_library_context(capture)
    url, headers = request_context(flow)
    report: dict[str, Any] = {
        "mode": "apply",
        "item_count": len(items),
        "album_count": len(desired),
        "captured_revision": captured_revision,
        "initial_revision": captured_revision,
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
    session = requests.Session()
    if proxy.casefold() == "direct":
        session.trust_env = False
        session.verify = True
    else:
        if not ca.is_file():
            raise ValueError(f"mitmproxy CA file not found: {ca}")
        session.proxies.update({"http": proxy, "https": proxy})
        session.verify = str(ca)

    revision, report["revision_refresh"] = refresh_revision(session, flow, signer, timeout)
    report["initial_revision"] = revision
    phase_one = [(item["cloud_miid"], 0 if item["desired_asco"] else 1) for item in items]
    revision, report["phase_one"] = send_phase(
        session, url, headers, revision, phase_one, signer, timeout
    )
    phase_two = [(item["cloud_miid"], item["desired_asco"]) for item in items]
    revision, report["phase_two"] = send_phase(
        session, url, headers, revision, phase_two, signer, timeout
    )
    report["final_revision"] = revision
    verify_revision, report["verification_revision_refresh"] = refresh_revision(
        session, flow, signer, timeout
    )
    expected = {item["cloud_miid"]: item["desired_asco"] for item in items}
    report["verification"] = verify_cloud_items(
        session,
        flow,
        signer,
        verify_revision,
        report["initial_revision"],
        expected,
        timeout,
    )
    return report


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().casefold() in {"y", "yes"}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="Live Library.musicdb; auto-detected on Windows and macOS")
    parser.add_argument("--capture", type=Path, help="Recent authenticated mitmproxy .mitm capture")
    parser.add_argument("--sap-signer", type=Path, help="Compatible Windows sapsigner.exe")
    parser.add_argument("--select", help="Album numbers, for example 1,3-5, or all")
    parser.add_argument("--all", action="store_true", help="Select every safe candidate")
    parser.add_argument("--list", action="store_true", help="List candidates and exit without writing")
    parser.add_argument("--dry-run", action="store_true", help="Build the plan without writing")
    parser.add_argument("--local-only", action="store_true", help="Skip Cloud Library synchronization")
    parser.add_argument(
        "--offline-copy",
        action="store_true",
        help="Treat --database as a detached copy and do not control Apple Music processes",
    )
    parser.add_argument("--backup-root", type=Path, default=Path.home() / "Music" / "Apple Music Split Albums Fixer Backups")
    parser.add_argument("--proxy", default="direct", help="Proxy URL, or direct")
    parser.add_argument("--ca", type=Path, default=Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--stop-apps", action="store_true", help="Stop Apple Music processes after confirmation")
    parser.add_argument("--restart", action="store_true", help="Start Apple Music when finished")
    parser.add_argument("--yes", action="store_true", help="Accept prompts; use with --select or --all")
    args = parser.parse_args()
    if args.all and args.select:
        parser.error("--all and --select cannot be used together")
    if args.yes and not (args.all or args.select or args.list):
        parser.error("--yes requires --all, --select, or --list")

    database = (args.database or default_database()).expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"database not found: {database}")
    print(f"Database: {database}")
    initial_scan, _ = scan_database(database)
    candidates = display_candidates(initial_scan)
    if args.list or not candidates:
        return 0

    if args.all:
        indexes = list(range(len(candidates)))
    elif args.select:
        indexes = parse_selection(args.select, len(candidates))
    else:
        indexes = parse_selection(
            input("\nSelect album numbers (example: 1,3-5) or type all: "),
            len(candidates),
        )
    chosen = [candidates[index] for index in indexes]
    chosen_keys = {candidate_key(candidate) for candidate in chosen}
    print("\nSelected:")
    for candidate in chosen:
        artist = candidate.get("album_artist") or candidate.get("artist") or "Unknown artist"
        print(f"- {candidate['album']} — {artist}")

    if args.dry_run:
        print("\nDry run complete. No files or cloud records were changed.")
        return 0

    capture: Path | None = None
    signer: Path | None = None
    if not args.local_only:
        capture = (args.capture or newest_capture())
        if capture is None:
            raise ValueError("no .mitm capture was found; pass --capture or use --local-only")
        capture = capture.expanduser().resolve()
        if not capture.is_file():
            raise ValueError(f"capture not found: {capture}")
        try:
            signer = resolve_sap_signer(args.sap_signer)
        except ValueError:
            if args.yes:
                raise
            entered = input("Path to sapsigner.exe: ").strip().strip('"')
            if not entered:
                raise ValueError(
                    "sapsigner.exe was not found; pass --sap-signer or set "
                    "APPLE_MUSIC_SAP_SIGNER"
                )
            signer = resolve_sap_signer(Path(entered))
        latest_library_context(capture)
        print(f"Cloud capture: {capture}")
        print(f"SAP signer: {signer}")

    if not confirm("Repair the selected albums and create a rollback backup?", args.yes):
        print("Cancelled. No files or cloud records were changed.")
        return 0

    processes = [] if args.offline_copy else running_music_processes()
    had_running_music = bool(processes)
    if processes:
        names = ", ".join(f"{name} ({pid})" for pid, name in processes)
        print(f"Running Apple Music processes: {names}")
        if not args.stop_apps:
            if args.yes:
                raise RuntimeError("Apple Music is running; close it or add --stop-apps")
            if not confirm("Stop these processes now?", False):
                raise RuntimeError("Apple Music must be stopped before repairing the live database")
        stop_music_processes(processes)

    stable_scan, stable_payload = scan_database(database)
    stable_by_key = {candidate_key(candidate): candidate for candidate in safe_candidates(stable_scan)}
    missing = chosen_keys - set(stable_by_key)
    if missing:
        raise RuntimeError("the database changed after scanning; run the tool again")
    stable_candidates = [stable_by_key[candidate_key(candidate)] for candidate in chosen]
    plan = [plan_candidate(candidate) for candidate in stable_candidates]

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_directory = args.backup_root.expanduser().resolve() / timestamp
    run_directory.mkdir(parents=True, exist_ok=False)
    backup_database = create_backup(database, run_directory)
    repaired_payload, changes = repair_payload(stable_payload, plan)
    transaction: dict[str, Any] = {
        "mode": "apply",
        "source": stable_scan["database"],
        "selected_album_filter": None,
        "planned_album_repairs": len(plan),
        "plan": plan,
        **changes,
        "output": None,
        "validation": None,
    }
    repaired_database = run_directory / "Library.repaired.musicdb"
    encoded = encode_musicdb(backup_database, repaired_payload)
    atomic_write(repaired_database, encoded)
    transaction["validation"] = validate_output(repaired_database, chosen_keys)
    transaction["output"] = {
        "path": str(repaired_database),
        "size": len(encoded),
    }
    transaction_path = run_directory / "local-repair-transaction.json"
    write_json(transaction_path, transaction)

    if not args.local_only:
        assert capture is not None and signer is not None
        cloud_ids(backup_database, transaction)

    atomic_write(database, repaired_database.read_bytes())
    validate_output(database, chosen_keys)
    print(f"\nLocal repair complete. Backup: {run_directory}")

    cloud_ok = True
    if not args.local_only:
        assert capture is not None and signer is not None
        try:
            cloud_report = synchronize_cloud(
                backup_database,
                transaction,
                capture,
                signer,
                args.proxy,
                args.ca.expanduser().resolve(),
                args.timeout,
            )
            write_json(run_directory / "cloud-sync-result.json", cloud_report)
            cloud_ok = bool(cloud_report["verification"]["verified"])
            verification = cloud_report["verification"]
            print(
                "Cloud verification: "
                f"{verification['matched_item_count']}/{verification['expected_item_count']} matched, "
                f"{len(verification['missing_cloud_miids'])} missing, "
                f"{len(verification['mismatches'])} mismatched."
            )
        except Exception as exc:
            cloud_ok = False
            failure = {"verified": False, "error": str(exc)}
            write_json(run_directory / "cloud-sync-error.json", failure)
            print(f"Cloud synchronization failed: {exc}", file=sys.stderr)

    should_restart = args.restart
    if had_running_music and not args.yes and not args.restart:
        should_restart = confirm("Start Apple Music now?", False)
    if should_restart:
        start_apple_music()
    print(f"Reports: {run_directory}")
    return 0 if cloud_ok else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
