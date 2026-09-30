/** Read a scan blob's footer from its `.groups.json` sidecar instead of parsing
 *  the ~5 MB thrift footer on a cold Worker isolate. `disk-tree index --to` /
 *  `reduce --to` precompute the sidecar (`disk_tree.find.groups`, spec
 *  `serverless-tier-reads.md`): the parquet schema + one compact array per row
 *  group — the pruning stats (`depth`/`path`/size min-max) and the stripped
 *  column-chunk offsets a range read needs, and nothing else.
 *
 *  `reviveMetadata` reconstructs a hyparquet `FileMetaData` from that, so the
 *  existing `selectRuns`/`readRows` consume it unchanged (the synthesized row
 *  groups carry `depth`/`path` statistics for pruning + the offsets for
 *  reading). A missing sidecar → `undefined` → the caller falls back to
 *  `parquetMetadataAsync` (`readRows(file, q)` with no metadata). Ports mgu's
 *  `_lib/index.ts` `reviveRowGroup`; dt uses the sidecar only, never D1. */
import type { FileMetaData } from 'hyparquet'

/** A `groups.json` schema leaf (hyparquet `SchemaElement` subset the writer emits). */
interface SchemaLeaf {
  type?: string
  repetition_type?: string
  name: string
  converted_type?: string
  num_children?: number
}

/** One row group, in `disk_tree.find.groups.GROUP_FIELDS` order:
 *  `[rg, d_min, d_max, p_min, p_max, b_max, u_min, u_max, row_start, row_end, rg_json]`
 *  where `rg_json` = `[num_rows, codec, [[data_page_offset, total_compressed_size,
 *  dictionary_page_offset|0], …]]` (one triple per leaf column, schema order). */
type GroupEntry = [number, number, number, string, string, number, string | null, string | null, number, number, string]

export interface GroupsDoc {
  v: number
  version: number
  schema: SchemaLeaf[]
  floor_bytes: number | null
  groups: GroupEntry[]
}

/** Size-column names, matching `disk_tree.find.groups.SIZE_COLS` (dt / mgu). */
const SIZE_NAMES = new Set(['size', 'b'])

/** `scans/<uuid>.parquet` → `scans/<uuid>.groups.json` (`groups.py:groups_path`). */
export const groupsKey = (blobKey: string): string => blobKey.replace(/\.parquet$/, '.groups.json')

/** The sidecar for `blobKey`, or `null` if absent (unindexed / older scan). */
export async function loadGroups(bucket: R2Bucket, blobKey: string): Promise<GroupsDoc | null> {
  const obj = await bucket.get(groupsKey(blobKey))
  if (!obj) return null
  const doc = JSON.parse(await obj.text()) as GroupsDoc
  return doc.v === 1 ? doc : null
}

/** Reconstruct a hyparquet `FileMetaData` from a sidecar: every row group, with
 *  the column-chunk offsets a read needs and `depth`/`path` statistics
 *  `selectRuns` prunes on. */
export function reviveMetadata(doc: GroupsDoc): FileMetaData {
  const leaves = doc.schema.slice(1) // [0] is the root element
  const row_groups = doc.groups.map(g => {
    const [, dMin, dMax, pMin, pMax, bMax, , , , , rgJson] = g
    const [numRows, codec, cols] = JSON.parse(rgJson) as [number, string, [number, number, number][]]
    if (cols.length !== leaves.length) throw new Error(`row group has ${cols.length} columns, schema ${leaves.length}`)
    const columns = cols.map(([dpo, size, dict], i) => {
      const name = leaves[i].name
      const statistics =
        name === 'depth' ? { min: dMin, max: dMax }
          : name === 'path' ? { min: pMin, max: pMax }
            : SIZE_NAMES.has(name) ? { max: bMax }
              : undefined
      return {
        meta_data: {
          type: leaves[i].type,
          path_in_schema: [name],
          codec,
          data_page_offset: BigInt(dpo),
          total_compressed_size: BigInt(size),
          ...(dict ? { dictionary_page_offset: BigInt(dict) } : {}),
          ...(statistics ? { statistics } : {}),
        },
      }
    })
    return { num_rows: BigInt(numRows), columns }
  })
  const num_rows = row_groups.reduce((s, rg) => s + Number(rg.num_rows), 0)
  return { version: doc.version, schema: doc.schema, num_rows: BigInt(num_rows), row_groups, metadata_length: 0 } as unknown as FileMetaData
}

/** `reviveMetadata(loadGroups(...))`, or `undefined` when the sidecar is absent —
 *  ready to hand straight to `readRows(file, q, meta)` / `readChunkPointers`. */
export async function metaFor(bucket: R2Bucket, blobKey: string): Promise<FileMetaData | undefined> {
  const doc = await loadGroups(bucket, blobKey)
  return doc ? reviveMetadata(doc) : undefined
}
