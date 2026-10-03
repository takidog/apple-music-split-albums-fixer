# Apple Music Split Albums Fixer

[繁體中文](README_zh.md)

A Python CLI for finding and repairing albums that Apple Music has split into multiple local album records. The current release supports Apple Music for Windows.

The main command scans the current library, lists only high-confidence repairs, lets you select albums by number, creates a complete rollback backup, repairs the local database, and synchronizes the selected changes to Cloud Library.

## Requirements

- Windows 10 or Windows 11
- Apple Music for Windows
- [uv](https://docs.astral.sh/uv/)
- for cloud synchronization, a recent authenticated mitmproxy capture and a compatible `sapsigner.exe`

Python packages are declared inside the scripts and installed automatically by `uv`.

## Interactive repair

From the project directory, run:

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py
```

The tool automatically finds the normal Windows library location and prints entries such as:

```text
Found 3 high-confidence split album(s):

[ 1] Branch — やなぎなぎ | 2 parts, 4 tracks, move 1
[ 2] Follow My Tracks — やなぎなぎ | 2 parts, 14 tracks, move 2
[ 3] Green Light — やなぎなぎ | 2 parts, 4 tracks, move 1

Select album numbers (example: 1,3-5) or type all:
```

After selection it:

1. validates the cloud capture and signer before changing the library;
2. closes Apple Music only after asking;
3. scans the stopped database again so the plan cannot use stale record IDs;
4. backs up the complete `.musiclibrary` directory;
5. repairs and parses a new database before installing it;
6. installs the validated database atomically;
7. sends signed Cloud Library edits for only the selected tracks;
8. reads the cloud delta and reports how many records matched, were missing, or differed.

The default backup and report directory is:

```text
%USERPROFILE%\Music\Apple Music Split Albums Fixer Backups\YYYYMMDD-HHMMSS
```

It contains the complete rollback copy, the local transaction report, the validated repaired database, and the cloud verification result.

## Useful modes

List current problems without writing anything:

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py --list
```

Preview a selection without writing:

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py --select 1,3-5 --dry-run
```

Repair every safe candidate and skip cloud synchronization:

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py --all --local-only
```

Supply the cloud inputs explicitly:

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py `
  --capture '.\captures\recent-sync.mitm' `
  --sap-signer 'C:\path\to\sapsigner.exe'
```

For automation, selection and consent must both be explicit:

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py `
  --select 1,2 `
  --yes `
  --stop-apps `
  --restart `
  --capture '.\captures\recent-sync.mitm' `
  --sap-signer 'C:\path\to\sapsigner.exe'
```

Use `--database` for a nonstandard library location. The program searches for the newest `.mitm` file in the project `captures` directory when `--capture` is omitted. It searches for a signer passed through `--sap-signer`, the `APPLE_MUSIC_SAP_SIGNER` environment variable, the project root, `tools\sapsigner.exe`, the development `work\vendor\ipatool-webGUI` checkout, and known Signum installation paths. An interactive run asks for the path if none of those locations contain it.

`--offline-copy` is available for testing a detached database copy without stopping or starting Apple Music. Do not use it for the live library.

## How the repair works

Apple Music's `Library.musicdb` is an encrypted and compressed binary database. The fixer:

1. validates the `hfma` envelope;
2. decrypts the AES-128 ECB prefix and decompresses the zlib payload;
3. parses every `itma` track and `iama` album record;
4. groups album objects by normalized album title, album artist, and artist;
5. marks a group safe only when one record has a unique majority, track numbers do not overlap, visible metadata is identical, and the combined sequence is contiguous;
6. reassigns the outlier tracks to the majority album object and removes unused album records;
7. updates record counts, section lengths, timestamps, and the outer envelope;
8. encrypts and parses the result again before replacing the live file.

Ties, overlapping or missing track numbers, and inconsistent metadata are reported as ambiguous and never offered for automatic repair.

Cloud synchronization maps the changed local track IDs to their Cloud Library `miid` values from the pre-repair backup. Each request receives a fresh SAP `X-Apple-ActionSignature`. The tool temporarily flips the compilation field, restores the intended value in a second request, then downloads the resulting `/items` delta and verifies every target.

## Rollback

Close Apple Music and `AMPLibraryAgent`, then replace the current `.musiclibrary` directory with the `backup` directory from the relevant timestamped run. Keep the backup until the repaired library has survived an Apple Music restart and another device has confirmed the cloud result.

## Advanced tools

The lower-level scripts remain available for inspection and manual workflows:

- `tools/musicdb_duplicate_scanner.py`: read-only JSON and Markdown scan reports;
- `tools/musicdb_duplicate_repair.py`: create a repaired database at a separate output path;
- `tools/sync_repaired_albums.py`: preview or apply a cloud transaction report.

The scanner, repair planner, binary encoder, and cloud synchronizer are Python modules without Windows UI dependencies. A future macOS version can reuse them and add macOS database discovery and Music process control.

Protocol details and reverse-engineering notes are kept in [research/README.md](research/README.md).

## Safety

- A live database is never repaired while Apple Music is running.
- A complete rollback copy is created before installation.
- Ambiguous duplicate groups are never changed automatically.
- Raw traffic captures can contain account tokens and library identifiers; keep them private.
- `sapsigner.exe` is an external component and is not distributed in this repository.

This is an independent interoperability project and is not affiliated with Apple Inc. Apple Music and related names are trademarks of their respective owners.
