import { expect, it } from 'vitest'
import { adminPaths, type AdminsData, emptyNotice } from './Admins'

const base: AdminsData = { list: true, staffDomain: null, table: 'ok', rows: [], bootstrap: [], none: false, grants: [], you: 'lead@example.org' }
const row = (email: string, ts: number, who = 'seed', note: string | null = null, github: string | null = null) => ({ email, note, who, ts, github })
const path = (p: Partial<ReturnType<typeof adminPaths>[number]>) => ({ github: null, note: null, addedBy: null, ts: null, you: false, removable: false, why: null, ...p })

it('the table lists every admin path: break-glass, the listed addresses, admin links; the caller is marked', () => {
  expect(adminPaths({
    ...base,
    bootstrap: ['root@example.org'],
    rows: [row('lead@example.org', 3), row('owner@example.org', 7, 'lead@example.org', 'org owner', 'octo-owner')],
    grants: [{ id: '0123456789abcdef', holder: null, email: null, scopes: ['gcs', 'admin'], createdBy: 'lead@example.org', createdAt: 9, expiresAt: null }],
  })).toEqual([
    path({ key: 'b:root@example.org', who: 'root@example.org', via: 'break-glass', addedBy: 'deploy config' }),
    path({ key: 'l:lead@example.org', who: 'lead@example.org', via: 'list', addedBy: 'seed', ts: 3, you: true, removable: true }),
    path({ key: 'l:owner@example.org', who: 'owner@example.org', via: 'list', github: 'octo-owner', note: 'org owner', addedBy: 'lead@example.org', ts: 7, removable: true }),
    path({ key: 'g:0123456789abcdef', who: 'link 01234567', via: 'link', note: 'gcs admin', addedBy: 'lead@example.org', ts: 9 }),
  ])
})

it('the last listed admin can\'t be removed while the list is the only way in; a break-glass address or the domain rule frees it', () => {
  const rows = [row('lead@example.org', 3)]
  const last = path({ key: 'l:lead@example.org', who: 'lead@example.org', via: 'list', addedBy: 'seed', ts: 3, you: true })
  expect([
    adminPaths({ ...base, rows }),
    adminPaths({ ...base, rows, bootstrap: ['root@example.org'] }).map(p => [p.key, p.removable]),
    adminPaths({ ...base, rows, list: false, staffDomain: 'example.org' }),
  ]).toEqual([
    [{ ...last, why: 'The last admin: add another before removing this one.' }],
    [['b:root@example.org', false], ['l:lead@example.org', true]],
    [path({ key: 'domain', who: 'everyone @example.org', via: 'staff domain', addedBy: 'deploy config', you: true }), { ...last, removable: true }],
  ])
})

it('an empty list says so, and how the reader is still an admin', () => {
  expect([
    emptyNotice({ ...base, rows: [row('lead@example.org', 3)] }),
    emptyNotice({ ...base, list: false, staffDomain: 'example.org' }),
    emptyNotice({ ...base, bootstrap: ['lead@example.org'] }),
    emptyNotice({ ...base, bootstrap: ['lead@example.org'], table: 'missing' }),
    emptyNotice({ ...base, you: null }),
  ]).toEqual([
    null,
    null,
    'The admin list is empty: add admins below. You are an admin by the break-glass setting (ADMIN_BOOTSTRAP), and nobody else is one by the list.',
    'The admin list is empty: its table isn’t in this database yet (apply the `admin-list` recipe), so nothing can be added until it is. You are an admin by the break-glass setting (ADMIN_BOOTSTRAP), and nobody else is one by the list.',
    'The admin list is empty: add admins below. You are an admin by a link, and nobody else is one by the list.',
  ])
})
