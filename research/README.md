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

## Cloud-sync result

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

The first direct `/edit` replay was rejected with HTTP 500 because it reused a captured signature. Cloud Library stayed at revision `20002495`, so that attempt caused no partial remote mutation.

The request includes `X-Apple-ActionSignature`. Captures show that Apple Music produces a unique 676-character Base64 value for every signed request. Its decoded representation is 507 bytes. A captured signature cannot be reused for a different body.

Apple's SAP signer accepts the exact request body and produces a fresh signature. A Windows `sapsigner.exe` implementation was tested offline first, then against the read-only Cloud Library `/update` endpoint. Its 501-byte signature was accepted with HTTP 200 even though Apple Music's native Windows signer emits 507 bytes.

The signed repair then succeeded:

- `Branch`: 2 Cloud Library items, revision `20002497` to `20002499` across the two phases;
- remaining 22 albums: 48 items, revision `20002499` to `20002501`;
- final readback at revision `20002503`: all 50 target `miid` values were present in the `/items` delta, all 50 had the expected `asco`, with zero missing values and zero mismatches.

This proves that the cloud mutation can be performed without reproducing Apple's private cryptography. The repairer signs each exact DMAP body through the SAP helper, sends the edit, checks the returned revision, and verifies the final `/items` delta.

## Web MusicKit and Mescal

The Apple Music web player exposes a developer JWT and a per-user media token. These are sufficient for the public `api.music.apple.com/v1/me/...` library operations, but the public API does not expose the album-compilation metadata edit needed for this repair.

The useful reference is [dado3212/apple-podcast-transcript-downloader](https://github.com/dado3212/apple-podcast-transcript-downloader). On macOS it asks `AMSMescal` to serialize selected URL/query/header fields and then calls `AMSMescalSession.signData`. This confirms that request canonicalization and SAP signing are separate stages.

For the Cloud Library DAAP endpoints tested here, Apple accepts an SAP signature over the exact HTTP body. The Windows helper used by [pdx15/ipatool-webGUI](https://github.com/pdx15/ipatool-webGUI) and the independently documented [lbr77/sap-unicorn](https://github.com/lbr77/sap-unicorn) follow the same model: establish a SAP session once, then sign arbitrary payload bytes locally. The private signing material stays inside Apple's FairPlay implementation rather than being exported as a reusable key.

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

External clients can register and read the Music domain. Reproducing the Media App lifecycle and the `SetProperties` command remains useful for a future capture-free workflow, but it is no longer required for cloud propagation because body signing now works directly.

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
