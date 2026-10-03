# Apple Music Split Albums Fixer

A Windows tool for finding and repairing Apple Music albums that have been split into two or more local album records.

This can happen when Apple Music resolves localized metadata inconsistently. Tracks from one release may end up attached to separate album objects even though the visible album title, album artist, and artist are the same. The result looks like multiple partial albums in the library.

## Platform and requirements

- Windows 10 or Windows 11
- Apple Music for Windows
- [uv](https://docs.astral.sh/uv/) for running the standalone Python scripts

The Python dependencies are declared inside each script and are installed by `uv` when needed.

## How it works

The fixer works on a clean, offline copy of `Library.musicdb`:

1. It validates the `hfma` database envelope.
2. It decrypts the AES-128 ECB prefix and decompresses the zlib payload.
3. It parses every `itma` track record and `iama` album record.
4. It groups album objects by normalized title, album artist, and artist.
5. It marks a group as safe to merge only when one album object has a unique majority of tracks, track numbers do not overlap, and the combined track sequence is contiguous.
6. It reassigns outlier tracks to the majority album object and removes the unused duplicate album records.
7. It updates the affected record counts, section lengths, timestamps, and outer database envelope.
8. It encrypts the repaired database and parses it again to confirm that every selected split is gone and all tracks remain readable.

Groups with ties, overlapping track numbers, missing track numbers, or other uncertain structure are reported as `ambiguous_duplicate` and are never repaired automatically.

## Back up the library

Quit both Apple Music and `AMPLibraryAgent` before copying the library. Back up the complete directory rather than only the database file:

```text
%USERPROFILE%\Music\Apple Music\Apple Music Library.musiclibrary
```

Keep this backup until the repaired library has been opened, restarted, and checked.

## Scan for split albums

Run the read-only scanner against the copied database:

```powershell
uv run --python 3.12 .\tools\musicdb_duplicate_scanner.py `
  '.\backups\pre-repair\Library.musicdb' `
  --json '.\reports\duplicate-albums.json' `
  --markdown '.\reports\duplicate-albums.md'
```

The JSON report contains the full record and track mapping. The Markdown report is easier to review manually. The scanner never writes to the database.

## Preview a repair

The repair command is a dry run unless `--apply` is supplied:

```powershell
uv run --python 3.12 .\tools\musicdb_duplicate_repair.py `
  '.\backups\pre-repair\Library.musicdb' `
  --report '.\reports\repair-dry-run.json'
```

Use `--album 'Exact album title'` one or more times to restrict the plan to selected albums.

## Create a repaired database

Writing requires both `--apply` and a separate `--output` path. The input file cannot be overwritten:

```powershell
uv run --python 3.12 .\tools\musicdb_duplicate_repair.py `
  '.\backups\pre-repair\Library.musicdb' `
  --apply `
  --output '.\work\Library.repaired.musicdb' `
  --report '.\reports\repair-transaction.json'
```

Review the transaction report before installing the repaired file. Stop Apple Music and `AMPLibraryAgent`, replace `Library.musicdb` with the repaired output, then start Apple Music and run the scanner once more against a new clean copy.

## Roll back

Stop Apple Music and `AMPLibraryAgent`, restore the complete backed-up `.musiclibrary` directory, and start Apple Music again.

## Cloud Library status

Cloud Library propagation is available on Windows through `tools/sync_repaired_albums.py`. It uses a compatible `sapsigner.exe` to generate a fresh `X-Apple-ActionSignature` for every request. The signer is not included in this repository.

The sync tool needs:

- the same clean pre-repair database used to create the repair;
- the JSON transaction report from `musicdb_duplicate_repair.py`;
- a recent mitmproxy capture containing an authenticated Apple Music `/update` request;
- a Windows SAP signer compatible with Apple's `X-Apple-ActionSignature` format.

Preview the exact tracks and Cloud Library IDs first:

```powershell
uv run --python 3.12 .\tools\sync_repaired_albums.py `
  '.\backups\pre-repair\Library.musicdb' `
  '.\reports\repair-transaction.json' `
  '.\captures\recent-sync.mitm' `
  --report '.\reports\cloud-sync-plan.json'
```

Apply the cloud repair only after reviewing that plan:

```powershell
uv run --python 3.12 .\tools\sync_repaired_albums.py `
  '.\backups\pre-repair\Library.musicdb' `
  '.\reports\repair-transaction.json' `
  '.\captures\recent-sync.mitm' `
  --apply `
  --sap-signer 'C:\path\to\sapsigner.exe' `
  --report '.\reports\cloud-sync-result.json'
```

The tool reads a fresh cloud revision, temporarily flips the compilation field on the repaired tracks, restores the intended value in a second signed request, and downloads the resulting `/items` delta. It reports success only when every target Cloud Library ID is present with the expected final value. Use `--album 'Exact album title'` or `--exclude-album 'Exact album title'` to limit a run.

Protocol notes, capture instructions, and the current cloud-sync work are documented in [research/README.md](research/README.md).

## Safety rules

- Never work on the live database while Apple Music is running.
- Always keep a complete rollback copy.
- Review `ambiguous_duplicate` groups manually.
- Treat raw traffic captures as private because they can contain account tokens and library identifiers.

This is an independent interoperability project and is not affiliated with Apple Inc. Apple Music and related names are trademarks of their respective owners.
