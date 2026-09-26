# ADR 0010 — Research catalog and vault enforcement

- **Status:** accepted; §4 superseded by ADR 0015 (gate tokens)
- **Date:** 2026-09-26
- **Tasks:** DATA-010 (DS-004 and GATE-002 build on it)

## Context

The vault (all data at or after `vault.start`, 2025-09-25T21:00Z) is the final out-of-sample test.
The plan makes it a date cutoff enforced in the catalog loader, "so it cannot be bypassed by
reading a different folder through the library", and gives `load_bars(..., *, allow_vault=False)`.
Gate tokens arrive with DS-004 (Sprint 3) and the one-time procedure with GATE-002 (Sprint 13).

## Decision

1. `xq.data.catalog.Catalog` is the only research-facing reader of market data. It queries the clean
   and bar Parquet stores with DuckDB and returns tz-aware UTC timestamps.
2. **Refuse, do not truncate.** A request whose `end` is after `vault.start` raises
   `VaultAccessError`. Silently cutting the range would let code believe it saw a full period.
3. **Defence in depth.** Queries also filter `ts_utc < vault.start` for ticks and
   `available_at_utc <= vault.start` for bars. A bar that straddles the boundary (for example the
   daily bar of the vault's first trading day) contains vault ticks and is never returned.
4. **No bypass yet.** `allow_vault=True` raises until DS-004 provides gate tokens; there is no
   interim flag. (Superseded: DS-004 replaced the flag with one-time gate tokens, ADR 0015.)
5. **Pipeline stages are not research reads.** Ingestion, cleaning, bar building and the quality
   checks process every partition, including vault ones, because the release gate needs validated
   vault data. They write derived stores and never hand data back to research code. Research
   statistics that feed modelling decisions — spread statistics for the cost model (ADR 0009) —
   use only pre-vault data.

## Consequences

- Research code can load up to `vault.start` and no further; the error message names the vault.
- Anything that reads Parquet files directly bypasses the catalog. That is allowed only inside the
  pipeline modules named above; reviews should treat any other direct read as a vault breach.
