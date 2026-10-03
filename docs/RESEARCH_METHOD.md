# Research method

## Evidence standard

Treat an HTTP `POST` as a write candidate, not proof of a write. Look for the request to follow a controlled library action, contain a relevant item or metadata change, receive an accepting response, and advance a Cloud Library revision. Some Apple endpoints use `POST` for reads, including revision polls and library queries.

The flow of interest observed during testing is:

1. `/edit` submits a library item change.
2. The server accepts the item and returns an updated revision.
3. The client requests `/items` for the new revision delta.

Catalog search and opening an already cached album can generate no library mutation. Separate those events from an actual metadata edit.

## Comparing locale runs

Use the same account, storefront, network, and sequence of actions. Change only the Music app language between runs, and fully restart the app. Compare locale headers and catalog responses, then check whether an item write follows. A language header by itself does not establish that localized metadata was uploaded.

## Reading local duplicates

The local scanner identifies multiple album records with normalized matching title, album artist, and artist, then examines which album persistent IDs the tracks reference. A majority album ID with complementary, contiguous track numbers is a useful split-album signal. Ties, overlapping numbers, or gaps remain ambiguous because they can represent editions or incomplete albums.

The scanner is read-only. Its proposed surviving album ID and outlier references should be reviewed against the actual release before attempting any repair.

## Privacy

Treat raw mitmproxy flows, library databases, screenshots, and unredacted sync exports as private. They may contain account tokens, library identifiers, artwork, listening metadata, or track details. Public bug reports should contain only the minimum redacted fields needed to reproduce the behavior.
