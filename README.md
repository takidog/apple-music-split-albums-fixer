# Apple Music split album research tools

Read-only tools for studying split album records and Cloud Library synchronization in Apple Music for Windows. The main tool scans a clean copy of `Library.musicdb` locally and reports album objects with matching visible metadata that are referenced by separate sets of tracks.

The scanner does not contact Apple, change the database, or generate write requests. Capture and local-first synchronization helpers are experimental and intended for controlled testing.

## Scan a library copy

Close Apple Music before making a consistent copy of the library. Then run the scanner against the copy:

```powershell
uv run --python 3.12 .\tools\musicdb_duplicate_scanner.py `
  '.\backups\Library.musicdb' `
  --json '.\reports\duplicate-albums.json' `
  --markdown '.\reports\duplicate-albums.md'
```

The scanner reads the `hfma` envelope, decrypts the AES-128 ECB prefix, decompresses the zlib payload, and parses track and album records. It groups album objects by normalized title, album artist, and artist, then compares the album IDs referenced by tracks.

Groups with matching metadata, one unique majority album ID, non-overlapping track numbers, and a contiguous combined sequence are labeled `likely_split_album`. Other duplicate groups are labeled `ambiguous_duplicate` for review. Suggested track-reference changes are informational only; `writes_generated` is always zero.

## Capture Apple Music traffic

Install mitmproxy and ensure `mitmweb.exe` is on `PATH`. Start a process-scoped capture from PowerShell:

```powershell
.\tools\start-capture.ps1 -Label 'startup' -TargetText 'Album title'
```

The capture targets the Apple Music app and its library agent using mitmproxy local capture mode. The web interface listens locally at `http://127.0.0.1:8081/`. Set `MITMWEB_PASSWORD` in the environment before starting if the local web interface needs a password.

Stop the capture and build a summary:

```powershell
.\tools\stop-capture.ps1
.\tools\build-report.ps1 -Label 'startup'
```

The observer redacts most header and query values in its JSONL index. Raw `.mitm` captures can contain authentication tokens, library identifiers, and private metadata. Keep them local and never attach unredacted captures to public issues.

## Local-first capture mode

For controlled testing, `-HoldLibraryWrites` intercepts requests to the observed Cloud Library `/edit` endpoint and returns a temporary `503` while leaving other traffic flowing. This can allow a local change to be observed before allowing the library agent to retry its cloud write.

```powershell
.\tools\start-capture.ps1 `
  -Label 'local-first' `
  -TargetText 'Album title' `
  -HoldLibraryWrites
```

After reviewing the local result, release the hold while the capture remains active:

```powershell
.\tools\release-library-writes.ps1
```

Keep the capture running until the write and following library delta are observed, then stop it. This mode is experimental; a held write may be retried or reported as failed by Apple Music. Back up the library before testing and verify the resulting cloud state afterward.

## Other tools

- `tools/export_sync_details.py` exports structural details from Cloud Library flows while hashing/redacting most values.
- `tools/plan_compilation_repairs.py` proposes outlier compilation values from an exported `/items` delta. It is read-only and generates no API writes.
- `tools/mark-event.ps1` records a timestamped manual action marker.

See [the research method](docs/RESEARCH_METHOD.md) for evidence and interpretation guidelines.

## Scope and disclaimer

This project studies possible localization and library-object causes of split albums. Similar symptoms can also result from different album editions, compilation settings, or inconsistent tags. A scan is diagnostic evidence, not proof that duplicate objects should be merged.

This is an independent interoperability research project and is not affiliated with or endorsed by Apple Inc. Apple Music, iTunes, and related names are trademarks of their respective owners.
