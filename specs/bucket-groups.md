# Named bucket groups: a Map view of several buckets

Ryan, 2026-11-24: the cw digest's weekly lines name pools like "08a zone", which are several buckets sharing one quota (hero, rhoarnet, 08a). They can't link anywhere, because no page shows those buckets together. Named bucket groups belong on `cloud`, available to every deployment, not just as cw config.

## What exists

- **The digest's `zone`** (job-side digest config, `buckets.<b>.zone`; site `src/digest/pools.ts`, Python `digest_quota._groups`). It groups buckets that share a quota, for the digest's figures only. The Map doesn't know about it.
- **Union views** (`STORES_JSON` entries with `union`, `functions/_lib/union.ts`, specs/federated-scans.md). These union whole stores' roots, not some of one store's buckets.
- **The Map's root:** one store's buckets as depth-1 children. There is no way to show a subset of them.

## What to build

1. **Config:** a store's `groups` in `STORES_JSON`, `{"<name>": {"label": "08a zone", "buckets": ["hero", "rhoarnet", "08a"]}}`. Each name is a URL-safe key, and the buckets must be ones the store's scans cover.
   - This config is the single source of truth.
   - The digest's `zone` names a group. `dt-cloud digest` checks, at config load, that every zone's members match its group's buckets, and fails loudly on a mismatch rather than drawing a different set.
2. **Map view `?g=<name>`:** the store's root restricted to the group's buckets.
   - The group is a synthetic root labelled by the group, with its buckets as children. Its size, object count and mtime are the members' sums.
   - Drilling into a bucket leaves the group view (the bucket's own page, with breadcrumbs `<root> › <group> › <bucket>`).
   - Every root-level read honors `g`:
     - `/api/subtree` at the root;
     - `/api/diff` at the root, for the "What changed" map and `?d=` links;
     - `/api/series` at the root, summed over members per scan, from the series rows the root already reads per bucket;
     - the children table.
   - Search (`?f=`) within a group filters to hits under its buckets.
   - Gates: `g` is a scope like `o=`/`cl=`. For a path-restricted link, a group containing a hidden bucket is refused, and `functions/gates.test.ts` gets the rows.
3. **Picker:** groups listed beside buckets in the Map's root picker or breadcrumb menu.
4. **Digest links:** the weekly lines and the bucket list link a pool to `?g=<name>` with the same diff window as a single bucket's link (`bucketLink` → a `groupLink`).
   - The quota reply's pool names link the same way.

## Notes

- The response cache key includes `g`; bump `RESPONSE_V` if any cached body's shape changes.
- The digest image (`/api/digest-image`) takes `g` so a pool can be drawn alone later. Not needed for the links.
- Parity: every deployment can declare groups.
  - cw declares its zones on adoption (`08a`, `rno2a`).
  - gcs, hf and r2 declare none unless a grouping is useful; config only, no code.

## Tests

- **`stores` parsing:** groups validated (unknown bucket, duplicate name, an empty group: each an error).
- **`/api/subtree` / `/api/diff` / `/api/series` with `g`:** exact sums over members, with non-members absent.
- **`gates.test.ts`:** `g` under a restricted link.
- **Digest:** a pool links to `?g=`, and a zone whose members disagree with its group fails config load.
