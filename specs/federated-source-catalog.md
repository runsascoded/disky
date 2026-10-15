# Project-owned source catalog — first implementation contract

Implemented parser/selection contract: [model]. Producer publication, remote fetch routing and source-reader integration remain implementation work. This is not a claim that Awair or the other projects already publish these artifacts.

## Ownership and routing

The producer owns its listing schedule, source credentials, derived artifacts, publication and retention. The aggregator gets only a deployment-configured read origin and completed catalog. Catalog JSON must not supply arbitrary remote origins or credentials; artifact references are relative controlled keys under the configured export origin/prefix. A catalog is not authorization to expose a source bucket.

`source` and `mount` are stable lowercase alphanumeric/hyphen identifiers. `scope.store` is an account-qualified physical data-store identity, for example `r2:rac/awair` (replace `rac` with the configured account identity); `scope.prefix` is empty for the whole bucket or a slash-terminated prefix. Reject duplicate sources/mounts or overlapping prefixes within the same physical store. Do not federate the legacy ctbk/crashes/jc-taxes union together with those same projects' independent exports.

## JSON shape

Illustrative fixture only: the zero digest and sizes below are placeholders, not a published Awair scan. Times must be valid UTC ISO instants (`.sss` optional on input, canonicalized on parse).

```json
{
  "version": 1,
  "source": "awair",
  "label": "Awair",
  "icon": "/icons/awair.svg",
  "mount": "awair",
  "scope": { "store": "r2:rac/awair", "prefix": "pyramid/" },
  "scans": [{
    "id": "2026-10-13T0800",
    "revision": "1",
    "digest": "0000000000000000000000000000000000000000000000000000000000000000",
    "complete": true,
    "observed_from": "2026-10-13T08:00:00Z",
    "observed_through": "2026-10-13T08:01:00Z",
    "published_at": "2026-10-13T08:02:00Z",
    "total_bytes": 123456,
    "total_objects": 12,
    "artifacts": {
      "format": "path-store-v2",
      "path": "scans/2026-10-13T0800/path.parquet",
      "bysize": "scans/2026-10-13T0800/bysize.parquet",
      "mtime": "scans/2026-10-13T0800/age.json"
    }
  }]
}
```

`icon` is optional: the source's mark on the picker's update chips and sources list, an absolute `http(s)://` URL or a path the serving site serves (`/…`); without one the picker shows `label`, which is also the icon's accessible name and tooltip. `path` is required; `bysize`, `mtime` and `search` are optional capability references. Absence means the capability is unavailable on that exact scan, not permission to fetch an older scan. A bare health-inventory response or UUID L2 listing is not a published path-store export. Serving also needs the producer's matching row-group/footer metadata/routing, using the existing path-store reader contract; the current catalog parser alone does not implement that reader or a cross-source D1 database.

Publish only successful, complete exports. All byte/object totals are nonnegative safe integers. IDs/revisions are nonempty strings; each `(id, revision)` is unique. The parser refuses pending/failed entries, invalid capture ordering (`from ≤ through ≤ published`), malformed hashes, absolute artifact URLs, traversal and encoded path segments. Artifact keys are producer-controlled names, not arbitrary source object keys.

The producer defines and records its logical-content digest algorithm over the complete exported snapshot and scope, including fields that affect served queries. The `digest` must be a lowercase SHA-256 hex identity, not an invented hash of the scan slug. That logical identity remains stable if storage files are relocated/compacted without changing the snapshot. Physical artifact checksums/routing may change independently. A corrected snapshot gets a new revision/digest; immutable revisions must not be overwritten in place. The first producer implementation must supply an actual verified digest and document the normalization; the TypeScript parser validates its shape, not authenticity or Parquet contents.

## Publication and readback

1. List the approved scope using the project's own credentials. Record start/end instants of the actual capture; indexing finish time is not a fabricated capture time.
2. Build the path/bysize and any advertised age/search artifacts using shared Disky tooling. Include all keys in the approved scope, including non-Parquet companion files and zero-byte objects. Preserve `pyramid/…` paths beneath the virtual Awair mount.
3. Publish immutable artifacts and their matching footer/routing metadata. Verify root totals and at least one directory drill through the intended source reader.
4. Only then publish the completed catalog entry. Serialize/conditionally update the small catalog to avoid concurrent producers dropping each other's entries; an upload/submission marker is not scan completion.
5. Preserve earlier revisions according to explicit retention. Advertise withdrawal; a pinned historical view then reports unavailable rather than selecting a different revision.

An aggregate root response is accepted only when it names the selected logical digest and its exact root byte/object totals match the catalog. The adapter mounts its ordinary tree nodes without adding source timestamps to the treemap's data contract. Missing reads produce partial coverage, not synthetic zero-sized sources. A common fleet budget must bound source responses; the same independent full budget must not be multiplied by the number of projects.

## Selection, pinning and timelines

At cutoff `T`, choose maximum `observed_through ≤ T`, resolving corrections by publication time then stable IDs/revisions. Later-published backfills can improve a cutoff-only view. For stable drill/diff/OG, persist the explicit source vector with `(id, revision, digest)`, mount and scope; do not re-resolve each endpoint separately. Unknown sources in a pin stay unknown even when backfills arrive. Missing pinned scans stay unavailable.

Diff uses the same picker/resolver twice, and counts only comparable source coverage. History uses union observation events, carrying prior observations forward and keeping pre-first-scan coverage null. Mtime rows preserve absolute object dates. Neither the shared picker nor these adapters is a scheduler abstraction.

## Local verification

```bash
pnpm --dir site exec vitest run src/federatedScans.test.ts src/federatedPickerModel.test.ts src/federatedViews.test.ts
pnpm --dir site exec tsc -b --noEmit
pnpm --dir site dev --host 127.0.0.1
FEDERATED_PREVIEW=1 BASE_URL=http://127.0.0.1:3263 pnpm --dir site exec playwright test federated-scans.spec.ts
```

Open [development fixture]. It uses fictional completed exports and a fixed fixture “latest” clock, makes no source listings, and is excluded from production route/bundle selection. It demonstrates three picker instances, exact URL cutoffs, mounted trees, coverage-aware root deltas and step-series values; it is not yet the production scan/date picker or a live federated reader.

[model]: ../site/src/federatedScans.ts
[development fixture]: http://127.0.0.1:3263/dev/federated-scans
