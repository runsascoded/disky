import { createContext, useContext, useMemo, useState } from 'react'
import { boolParam, useUrlState } from 'use-prms'
import { fmtBytesIec, fmtBytesLike, fmtBytesSi } from './types'
import type { Units } from './types'

// Byte-unit display preference. SI (`T`, powers of 1000) is the default on
// every deployment — it's what bills, quotas and dashboards mostly quote, and
// what readers expect without thinking about it; binary IEC (`Ti`) is a
// per-browser choice (the ☰ / avatar menu's "Display" submenu, or `i`). The
// precedence: an explicit URL param (`?iec`, or `?si` — now just the default
// spelled out; shareable), the persisted choice (localStorage), then SI.
// Choosing IEC writes `?iec` too, so a copied URL carries it; choosing SI
// clears both params, so plain links stay clean.
//
// The trailing "B" is its own preference on the same ladder (`?rb` param >
// localStorage > default): bare `1.2 T` is the default — tighter in dense
// treemap cells — with `?rb` ("restore B") / the persisted choice bringing
// back `TB`/`TiB`.
const KEY = 'units'
const read = (k: string): string | null => {
  try { return localStorage.getItem(k) } catch { return null }
}
const load = (): Units | null => {
  const v = read(KEY)
  return v === 'si' || v === 'iec' ? v : null
}
const BKEY = 'unitsB'
const loadB = (): boolean | null => {
  const v = read(BKEY)
  return v === '1' ? true : v === '0' ? false : null
}

interface UnitsCtx {
  units: Units
  suffixB: boolean
  fmtBytes: (b: number) => string
  /** `b` rendered at the unit `ref` would take — one unit across a list. */
  fmtBytesLike: (b: number, ref: number) => string
  setUnits: (u: Units) => void
  setSuffixB: (b: boolean) => void
  toggleUnits: () => void
  toggleSuffixB: () => void
}

const Ctx = createContext<UnitsCtx>({
  units: 'si',
  suffixB: false,
  fmtBytes: (b: number) => fmtBytesSi(b, false),
  fmtBytesLike: (b: number, ref: number) => fmtBytesLike(b, ref, 'si', false),
  setUnits: () => {},
  setSuffixB: () => {},
  toggleUnits: () => {},
  toggleSuffixB: () => {},
})

export function UnitsProvider({ children }: { children: React.ReactNode }) {
  const [siP, setSiP] = useUrlState('si', boolParam)
  const [iecP, setIecP] = useUrlState('iec', boolParam)
  const [rbP, setRbP] = useUrlState('rb', boolParam)
  const [pref, setPref] = useState<Units | null>(load)
  const [bPref, setBPref] = useState<boolean | null>(loadB)
  const units: Units = iecP ? 'iec' : siP ? 'si' : (pref ?? 'si')
  const suffixB = rbP ? true : (bPref ?? false)
  const value = useMemo<UnitsCtx>(() => {
    const setUnits = (next: Units) => {
      try { localStorage.setItem(KEY, next) } catch { /* in-memory only */ }
      setPref(next)
      setIecP(next === 'iec')
      if (siP) setSiP(false)
    }
    const setSuffixB = (next: boolean) => {
      try { localStorage.setItem(BKEY, next ? '1' : '0') } catch { /* in-memory only */ }
      setBPref(next)
      setRbP(next)
    }
    return {
      units,
      suffixB,
      fmtBytes: (b: number) => (units === 'iec' ? fmtBytesIec : fmtBytesSi)(b, suffixB),
      fmtBytesLike: (b: number, ref: number) => fmtBytesLike(b, ref, units, suffixB),
      setUnits,
      setSuffixB,
      toggleUnits: () => setUnits(units === 'si' ? 'iec' : 'si'),
      toggleSuffixB: () => setSuffixB(!suffixB),
    }
  }, [units, suffixB, siP, setSiP, setIecP, setRbP])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useUnits = () => useContext(Ctx)
