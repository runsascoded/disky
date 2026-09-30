import { flexRender, getCoreRowModel, getSortedRowModel, useReactTable } from '@tanstack/react-table'
import type { ColumnDef, SortingState } from '@tanstack/react-table'
import { useMemo, useState } from 'react'

import type { ColumnSpec, Row, TableMeta } from './db'
import { useTable, useTableMutations } from './db'
import { Skeleton } from './Busy'

// Generic editable table over one /api/db table: TanStack Table for the
// grid (sorting now; filtering/pagination are config away), TSQ mutations
// for cell edits, row adds, and deletes. Adding another table to the admin
// dashboard is a registry entry in functions/_lib/tables.ts — this component
// renders whatever the server describes. Read-only for viewers without the
// table's write scope, which is what makes e.g. the email whitelist shareable.

const fmtCell = (c: ColumnSpec, v: string | number | null): string => {
  if (v == null) return ''
  if (c.name === 'ts' && typeof v === 'number') return new Date(v * 1000).toLocaleString()
  return String(v)
}

function EditableCell({ value, commit }: { value: string; commit: (v: string) => void }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)
  if (!editing) {
    return (
      <span className="cell editable" title="click to edit" onClick={() => { setDraft(value); setEditing(true) }}>
        {value || <em className="empty">—</em>}
      </span>
    )
  }
  const done = (save: boolean) => {
    setEditing(false)
    if (save && draft !== value) commit(draft)
  }
  return (
    <input
      autoFocus
      value={draft}
      onChange={e => setDraft(e.target.value)}
      onBlur={() => done(true)}
      onKeyDown={e => {
        if (e.key === 'Enter') done(true)
        if (e.key === 'Escape') done(false)
      }}
    />
  )
}

export function DbTable({ name }: { name: string }) {
  const { data, error, isPending } = useTable(name)
  const { insert, update, remove } = useTableMutations(name)
  const [sorting, setSorting] = useState<SortingState>([])
  const [draft, setDraft] = useState<Record<string, string>>({})

  const spec: TableMeta | undefined = data?.spec
  const columns = useMemo<ColumnDef<Row>[]>(() => {
    if (!spec) return []
    const cols: ColumnDef<Row>[] = spec.columns.map(c => ({
      accessorKey: c.name,
      header: c.name,
      cell: ({ row, getValue }) => {
        const v = getValue() as string | number | null
        if (spec.canWrite && c.editable) {
          return (
            <EditableCell
              value={v == null ? '' : String(v)}
              commit={value => update.mutate({ pk: String(row.original[spec.pk]), col: c.name, value })}
            />
          )
        }
        return fmtCell(c, v)
      },
    }))
    if (spec.canWrite) {
      cols.push({
        id: '_actions',
        header: '',
        enableSorting: false,
        cell: ({ row }) => (
          <button type="button" className="rm" title="delete row" onClick={() => remove.mutate(String(row.original[spec.pk]))}>
            ✕
          </button>
        ),
      })
    }
    return cols
  }, [spec, update, remove])

  const table = useReactTable({
    data: data?.rows ?? [],
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  if (error) return <p className="err">{error.message}</p>
  if (isPending) return <Skeleton height={200} label="loading table…" />
  if (!spec) return null

  const addable = spec.columns.filter(c => !c.server)
  const err = insert.error ?? update.error ?? remove.error

  return (
    <div className="db-table">
      {spec.canWrite && (
        <form
          className="add-row"
          onSubmit={e => {
            e.preventDefault()
            insert.mutate(draft, { onSuccess: () => setDraft({}) })
          }}
        >
          {addable.map(c => (
            <input
              key={c.name}
              value={draft[c.name] ?? ''}
              onChange={e => setDraft(d => ({ ...d, [c.name]: e.target.value }))}
              placeholder={c.name + (c.required ? '' : ' (optional)')}
              required={c.required}
            />
          ))}
          <button type="submit" disabled={insert.isPending}>Add</button>
        </form>
      )}
      {err && <p className="err">{err.message}</p>}
      <table>
        <thead>
          {table.getHeaderGroups().map(hg => (
            <tr key={hg.id}>
              {hg.headers.map(h => (
                <th
                  key={h.id}
                  className={h.column.getCanSort() ? 'sortable' : ''}
                  onClick={h.column.getToggleSortingHandler()}
                >
                  {flexRender(h.column.columnDef.header, h.getContext())}
                  {{ asc: ' ▲', desc: ' ▼' }[h.column.getIsSorted() as string] ?? ''}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map(r => (
            <tr key={r.id}>
              {r.getVisibleCells().map(c => (
                <td key={c.id}>{flexRender(c.column.columnDef.cell, c.getContext())}</td>
              ))}
            </tr>
          ))}
          {!table.getRowModel().rows.length && (
            <tr><td colSpan={columns.length}><em>empty</em></td></tr>
          )}
        </tbody>
      </table>
      <p className="row-count">{data.rows.length} row{data.rows.length === 1 ? '' : 's'}{spec.canWrite ? '' : ' (read-only)'}</p>
    </div>
  )
}
