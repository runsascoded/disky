"""Shared bits for the opt-in aggregation extensions (spec: specs/done/aggregation-extensions.md).

- ``--pivot-sum <col>``: per-category byte sums as layer-2 columns
  ``sum_<col>_<v>`` (files: own value; dirs: bottom-up sum). For enums like
  storage class — cardinality-guarded.
- ``--mean-mtime``: size-weighted mean mtime (``mtime_mean``,
  ``Σ mtime·size / Σ size`` over descendant files; zero-byte files contribute
  0 to both terms). Files carry their own mtime; dirs with zero total size
  get NULL.

Cross-engine exactness: the weighted sum ``Σ mtime·size`` overflows int64
(~1.7e21 at PB scale) and float summation is order-dependent, so every engine
carries it as an exact integer (Python bigints / DuckDB HUGEINT) through the
cascade and performs a single ``double(wsum) / double(size)`` division at the
end — the same two correctly-rounded conversions + one IEEE division
everywhere, so ``mtime_mean`` is byte-identical across engines.
"""

from __future__ import annotations

# Pivoting is for enums (storage classes: ≤ ~10 values); a high-cardinality
# column here is a mistake (free-form values would explode the schema).
PIVOT_MAX = 32

# Internal cascade column for the exact Σ mtime·size partial; never emitted.
MT_WSUM = 'mt_wsum'
MTIME_MEAN = 'mtime_mean'

# ``--size-hist`` (spec specs/done/mgu-scale-unification.md item E): per path, a log2
# histogram of descendant files by size — bin 0 holds zero-byte files, bin b
# (1 ≤ b < SIZE_HIST_BINS) holds sizes in [2^(b−1), 2^b), and the last bin
# is open-ended (≥ 2^(SIZE_HIST_BINS−2) = 512 GiB). Additive through the
# cascade: one count column and one byte column per bin ride `sum_cols`, and
# the output packs them into two LIST(BIGINT) columns.
SIZE_HIST_BINS = 41
SIZE_HIST_N = 'size_hist_n'
SIZE_HIST_BYTES = 'size_hist_bytes'


def size_bin(size: int) -> int:
    """The histogram bin of a file size (the Python twin of the SQL in the duckdb engine)."""
    if size < 0:
        raise ValueError(f"negative size {size}")
    if size == 0:
        return 0
    return min(size.bit_length(), SIZE_HIST_BINS - 1)


def size_hist_cols() -> tuple[list[str], list[str]]:
    """Cascade column names: (`sh_n_<b>`…, `sh_b_<b>`…)."""
    return (
        [f'sh_n_{b}' for b in range(SIZE_HIST_BINS)],
        [f'sh_b_{b}' for b in range(SIZE_HIST_BINS)],
    )


def pivot_col(col: str, v) -> str:
    return f'sum_{col}_{v}'


def check_pivot_values(col: str, values: list) -> list:
    if len(values) > PIVOT_MAX:
        raise ValueError(
            f"--pivot-sum {col}: {len(values)} distinct values exceeds the cap of {PIVOT_MAX} — "
            f"pivoting is for enum-like columns (e.g. storage class), not free-form ones"
        )
    return values


def mean_of(wsum: int, size: int) -> float | None:
    """double(wsum) / double(size) — the one blessed division (see module doc)."""
    return float(wsum) / float(size) if size > 0 else None
