# Synthetic MT5 tick exports

Deterministic, synthetic XAUUSD tick exports in MT5 "Export ticks" format and broker server time
(`NY+7`: UTC+2 in US winter, UTC+3 in US summer). They are **not market data**. They exist to test
ingestion and timestamp normalization, and to exercise `xq ingest` end to end.

| File | Spans | Purpose |
| --- | --- | --- |
| `XAUUSD_mt5_ticks_2024-03-06_2024-03-13.csv` | 2024-03-06 to 2024-03-13 UTC | US DST start (Sun 10 Mar), before the EU change |
| `XAUUSD_mt5_ticks_2024-10-30_2024-11-06.csv` | 2024-10-30 to 2024-11-06 UTC | US DST end (Sun 3 Nov), after the EU change |

Ticks exist only while the synthetic broker quotes: not from Friday 17:00 to Sunday 18:00 New York,
and not from 17:00 to 18:00 New York each day. Each row carries only the changed side(s), as real
MT5 exports do.

Regenerate (the test suite checks the files match the generator byte for byte):

```bash
PYTHONPATH=tests uv run python -m helpers.mt5_fixtures
```
