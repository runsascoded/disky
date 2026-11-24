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

## As built (items 1–3)

Branch `rw/bucket-groups`. Item 4 (digest links) is not built yet; see "Left for item 4".

### Config (`site/functions/_lib/stores.ts`)

- A secondary store's entry takes `groups`. The primary store isn't an entry in `STORES_JSON`, so its groups sit under the reserved key `primary`, which takes `groups` and nothing else: `{"primary": {"groups": {"pool-a": {"label": "pool A", "buckets": ["alpha", "beta"]}}}}`. `store=primary` still means the primary, and no secondary store can be named `primary`.
- Validation at parse: a name is a store key's shape (`^[a-z0-9][a-z0-9-]*$`); a group is exactly `{label, buckets}`, with a nonempty label, at least one bucket, and no bucket empty, containing `/`, or listed twice.
- "Duplicate name" can't happen in parsed JSON, since `JSON.parse` keeps the last of two equal keys. The checked duplicates are two groups sharing a label (the picker couldn't tell them apart) and one bucket listed twice in a group.
- "Unknown bucket": checked where the store's env is known (`storeGroups(env)`), against its `STORE_BUCKETS` when that var is set. Without it, nothing at config time can say which buckets the scans cover. A member that a scan doesn't hold is simply absent from that scan's group view, the way a bucket is absent before it joined the scan set.
- The Python check (`dt-cloud digest`: every zone's members match its group) is not built. The digest modules were off limits for this change, and the job doesn't read `STORES_JSON` today, so the check first needs the site's group config to reach the job.

### Where `g` threads (`site/functions/_lib/groups.ts`)

`g` acts at the root only (`path` empty). Below the root it is ignored, so no cache key below the root moves. At the root, each route builds its answer the way a union root does: from each member bucket's own read (`path=<bucket>`, no `g`) through the route's ordinary read path, then merges the answers.

- **`/api/subtree`** (`groupSubtree`):
  - First pass: each member's `depth=1` read, which is the same request a drill into that bucket makes. Under `q`, the member's filtered read instead.
  - The members' totals give the group's byte threshold, `Σ bytes · minArea / (w·h)`.
  - `depth=1` answers with the members alone.
  - Otherwise, second pass: each member is read at `threshold · atten` (`&T=` in its key). A bucket's children are the group root's grandchildren, so the threshold is attenuated one level. `depth=N` passes `N − 1` to the members.
  - The root is `groupRoot`: the members sorted largest first under the group's label. Its bytes and objects are the members' sums; its non-Standard class bytes, owners and unknown-owner bytes are summed; its mtime and last read are the members' newest.
  - Under a scope (`lens`, `o`, `cl`, `q`), a member the scope leaves empty is absent. In the plain view, an empty member still appears.
  - Under `q`, the filter fields merge with `unionFilterFields`, each member named as the "store".
  - The body adds `group: {name, label, buckets}`.
- **`/api/diff`** (`groupDiff`):
  - First pass: each member's `summary=1` diff. These give the totals and the threshold (from the larger side).
  - `summary=1` answers with the totals and no rows.
  - `depth=1` answers with one row per member, built from its totals.
  - Otherwise, second pass: each member with bytes or objects on either side is diffed at `threshold · atten`.
  - The rows come from `groupDiffRows`. Each member gets a depth-1 row with its totals, marked `expanded` when its own rows came too. Its rows follow as `<bucket>/<row>`, one level deeper, with its `(other)` becoming `<bucket>/(other)`.
  - Every member a scan holds gets its depth-1 row, an unchanged one included (zero delta, drawn as any unchanged row is). A group is an explicit set of buckets and its totals count every member's bytes, so an unchanged member isn't dropped as filler the way an unchanged bucket is at the store root (cw, 2026-11-24: `rhoarnet-us-east-08a` was missing from `?g=08a&d=-7d`).
  - A member empty on both sides isn't diffed again in the second pass; it keeps its totals and has no rows below. Under a scope (`lens`, `o`, `cl`, `q`), a member the scope leaves empty on both sides is absent, as in the group's subtree.
  - A member neither scan holds (its diff is a 404) is absent.
- **`/api/series`** (`groupSeries`):
  - `split=roots` (what the map's root chart asks for): the store root's own `split=roots` read. Its per-bucket traces are the rows the root already reads per bucket. The members' traces are kept, and the points are their per-scan sums.
  - `paths=` (a filter's match roots): answered as without `g`, after checking that every path is under a member (otherwise 400).
  - Anything else: each member's own series (`path=<bucket>`, with its scope axes and `chunk`), summed per scan (`sumPoints`). A member with no point at a scan adds nothing.
  - A store's `series_scope` admits only `store=`, so it never reads a group.
- **The children table** needs no change: at the group root, its node's children are the members. `flatOk` (the `?tm=` largest-anywhere listing) is keyed on the scope axes without `g`, so a bucket drilled under a group keeps it.
- **Union views:** a union root refuses `g` (400, alongside `lens`/`o`/`cl`). Below a union's root, `g` reaches the member, which ignores it.
- **`/api/groups`** (new, viewer gate): the store's groups in config order. It's for the picker, since `/api/filter-caps` is ungated and group buckets are store data.

### Gates

- `g` is a scope, as `o=`/`cl=` are. A path-restricted link is refused (403, `code: restricted-group`, `restrictedGroupRefused`) on a group with a member at or above a hidden dir (`groupReachesHidden`, which reuses `scopeReachesHidden`). That covers a hidden prefix inside a member bucket as well as a hidden bucket. `/api/groups` leaves such groups out for that link.
- `functions/gates.test.ts`:
  - New cases: `g=clear` on each route behind the viewer gate, and `/api/groups`.
  - New table (`GROUP_EXPECT`): open link vs restricted link on `g=clear` / `g=holds` for subtree, diff and series. It also checks that `g` below the root is ignored.
  - The `/api/groups` listing is checked for each link.

### Cache keys

The group's merged answer is not cached itself, as a union root's isn't. Its members' reads are cached under their own keys. The first passes reuse exactly the keys a drill or a summary already uses. The second passes add `&T=`, as a union member's reads do. No existing key or cached body shape changed, so `RESPONSE_V`/`CACHE_V` stay as they were.

### Picker and crumbs (`site/src/App.tsx`)

- `?g=` joins the map's `scopeQs`, so every subtree and diff request carries it (the server ignores it below the root) and the views' query keys differ per group. `SizeOverTime` takes `group` and sends `&g=` at the root.
- Crumbs: `<root> › <group> / <bucket> / …`.
  - The root crumb drops `g`, and shows the deployment's label, not the group root's.
  - The group crumb returns to the group root, keeping `g`. Its tooltip lists the members.
- At the root, a select (`Bucket group`) lists the store's groups from `/api/groups`, plus "all buckets". It is shown only when the store has groups.
- The age chart (`/api/age-pyramid`) is not group-scoped. At a group root it still shows the whole store.

### Left for item 4

- Today `bucketLink(bucket, r, since, cfg)` (`site/src/digest/quota.ts`) renders `[label](diffUrl(r.scan, dlink(r), since, cfg.site_url, path))`, and `diffUrl` builds `${site}/${bucket}?d=${slug}${-span}#over-time`.
- `groupLink(group, label, r, since, cfg)` should call `diffUrl` the same way, with `bucket = ''` and then `&g=<group>` added, giving `${site}/?d=${slug}${-span}&g=${group}#over-time`. Alternatively, `diffUrl` could take a `g` argument and put it before the `#`.
- That gives a group view at `/?g=<name>&d=<end>`, and a group diff at `/?g=<name>&d=<end>-<span or start>#over-time`, with the same diff window as a single bucket's link.
- The quota reply's pool names (`quotaReply.ts`, the `zone` pools) call it with the zone's group name. The digest's `zone` then names a group.

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
