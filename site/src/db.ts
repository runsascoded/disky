// TSQ bindings for the generic /api/db table surface (see functions/api/db).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

export interface ColumnSpec {
  name: string
  type: 'text' | 'int'
  editable?: boolean
  server?: 'who' | 'now'
  required?: boolean
}

export interface TableMeta {
  name: string
  pk: string
  columns: ColumnSpec[]
  desc: string
  canWrite: boolean
}

export type Row = Record<string, string | number | null>

async function req(url: string, init?: RequestInit): Promise<unknown> {
  const r = await fetch(url, { credentials: 'include', ...init })
  const body = (await r.json().catch(() => ({}))) as { error?: string }
  if (!r.ok) throw new Error(body.error ?? `${r.status}`)
  return body
}

export function useTables() {
  return useQuery<TableMeta[], Error>({
    queryKey: ['db', 'tables'],
    retry: false,
    queryFn: async () => ((await req('/api/db')) as { tables: TableMeta[] }).tables,
  })
}

export function useTable(name: string) {
  return useQuery<{ spec: TableMeta; rows: Row[] }, Error>({
    queryKey: ['db', name],
    retry: false,
    queryFn: async () => (await req(`/api/db/${name}`)) as { spec: TableMeta; rows: Row[] },
  })
}

/** insert / update-cell / delete — all invalidate the table's query. */
export function useTableMutations(name: string) {
  const qc = useQueryClient()
  const done = () => void qc.invalidateQueries({ queryKey: ['db', name] })
  const insert = useMutation({
    mutationFn: (values: Record<string, string>) =>
      req(`/api/db/${name}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ values }) }),
    onSuccess: done,
  })
  const update = useMutation({
    mutationFn: (v: { pk: string; col: string; value: string }) =>
      req(`/api/db/${name}`, { method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify(v) }),
    onSuccess: done,
  })
  const remove = useMutation({
    mutationFn: (pk: string) => req(`/api/db/${name}?pk=${encodeURIComponent(pk)}`, { method: 'DELETE' }),
    onSuccess: done,
  })
  return { insert, update, remove }
}
