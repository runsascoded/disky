/**
 * Exact, platform-stable `10 ** k` for integer `k`.
 *
 * `**` / `Math.pow` are implementation-approximated, not correctly rounded:
 * `10 ** -4` is `0.0001` on some engines and `0.00009999999999999999` on
 * others (this bit CI on decade tick values). Repeated multiplication is
 * exact through 1e22, and dividing 1 by an exact integer is correctly
 * rounded — so this always returns the same double as the decimal literal.
 */
export function pow10(k: number): number {
  let v = 1
  const n = Math.abs(Math.trunc(k))
  for (let i = 0; i < n; i++) v *= 10
  return k < 0 ? 1 / v : v
}
