# Apple Music synchronization research

This document contains the protocol and reverse-engineering notes used while building Apple Music Split Albums Fixer. The supported repair workflow is documented in the project root [README](../README.md).

## Observed data flow

Apple Music for Windows stores the local library in an encrypted and compressed `Library.musicdb`. The UI process and `AMPLibraryAgent.exe` have separate roles:

1. Apple Music reads and edits the local library through Library Agent commands.
2. `AMPLibraryAgent.exe` reconciles local changes with Cloud Library.
3. A cloud metadata edit is submitted to `librarydaap.itunes.apple.com` through a DMAP `/edit` request.
4. The server returns a new library revision.
5. The client requests the matching `/items` revision delta.

A `POST` alone is not evidence of a successful write. A valid mutation must have an accepting response, a higher revision, and a matching `/items` delta.

## Split-album finding

The tested failure created multiple local `iama` album records with identical visible metadata. Different `itma` track records referenced different album persistent IDs. Apple Music's ordinary "album is a compilation" edit merged the records, updated the track-to-album reference, removed the unused album object, and later emitted a Cloud Library edit.

The production repairer reproduces the local structural change while leaving uncertain duplicate groups untouched.

## Current cloud-sync result

The local repair mapped all 50 changed local track IDs in the test library to unique Cloud Library `miid` values. The corresponding DMAP body uses:

```text
mebs
  mstc  request timestamp
  mlid  database ID
  musr  current revision
  mikd  item kind
  mlcl
    mlit
      miid  Cloud Library item ID
      asco  compilation value
```

Direct `/edit` replay was rejected with HTTP 500. Cloud Library stayed at revision `20002495`, so no partial remote mutation occurred.

The request includes `X-Apple-ActionSignature`. Captures show that the value differs between `/update` and `/edit`; copying an authenticated header set from a nearby request does not work. The leading hypotheses are:

1. the signature covers the HTTP method, path, body, timestamp, and account/session context;
2. it is generated inside Apple Music's networking stack or `AMPLibraryAgent.exe` immediately before dispatch;
3. invoking the official Library Agent command path is more reliable than recreating the encoder and key handling.

## Library Agent interface

The installed app exposes an out-of-process COM/WinRT server:

```text
Server CLSID:   68E7097C-F969-4006-AAC3-95115F0ED1C4
IAMPLibrary:    F707A913-E0CE-4FD4-BCE3-425DD153285B
Proxy DLL:      AMPLibraryAgent.Proxies.dll
Metadata:       AMP.Core.winmd
```

Relevant methods include:

```text
MediaAppExecAgentCommandAsync
RegisterLibraryClient
OpenDomainsAsync
SendDBChangesToLibraryAsync
FetchLibraryFromRevsionAsync
```

External clients can register and read the Music domain. The remaining work is to reproduce the Media App lifecycle and send a documented `SetProperties` command so Library Agent creates its normal change journal and signs the resulting `/edit` request.

The command packer uses the keys `command-id` and `command-params`. `SetProperties` expects database identifiers, property IDs, and serialized property values. Its exact parameter encoding is still being decoded.

## Capture Apple Music traffic

Install mitmproxy, then start a process-scoped capture:

```powershell
.\tools\start-capture.ps1 -Label 'startup' -TargetText 'Album title'
```

The capture targets `AppleMusic.exe` and `AMPLibraryAgent.exe` through mitmproxy local-capture mode. The web interface is available at `http://127.0.0.1:8081/`.

Record manual events when comparing UI actions:

```powershell
.\tools\mark-event.ps1 -Event 'before_edit' -Note 'Compilation toggle'
.\tools\mark-event.ps1 -Event 'after_edit'
```

Stop and summarize the capture:

```powershell
.\tools\stop-capture.ps1
.\tools\build-report.ps1 -Label 'startup'
```

Raw `.mitm` captures can contain authentication tokens, library identifiers, and private metadata. Keep them local.

## Local-first capture mode

The observer can temporarily return `503` for Cloud Library `/edit` while allowing other traffic to continue:

```powershell
.\tools\start-capture.ps1 `
  -Label 'local-first' `
  -TargetText 'Album title' `
  -HoldLibraryWrites
```

After confirming the local result, release writes while the capture is still running:

```powershell
.\tools\release-library-writes.ps1
```

Keep the capture running until `/edit`, `/update`, and the `/items` delta have completed. The stop script refuses to stop while the hold marker is present.

## Locale comparison method

Use the same account, storefront, network, album, and UI sequence. Change only the Apple Music app language, restart the app fully, and compare:

- `Accept-Language`, `X-Apple-I-Locale`, `X-Apple-Locale`, and related headers;
- localized catalog metadata returned for the same IDs;
- the local album and track records created after catalog reconciliation;
- whether a signed `/edit` follows the local change;
- the accepted revision and subsequent `/items` delta.

Additional evidence rules are in [docs/RESEARCH_METHOD.md](../docs/RESEARCH_METHOD.md).
