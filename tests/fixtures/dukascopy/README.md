# Synthetic Dukascopy tick files

Deterministic, synthetic XAUUSD ticks in the two formats the `dukascopy` source reads (DATA-013,
ADR 0057). They are **not market data**. Timestamps are UTC, as Dukascopy writes them.

| Path | Format | Spans (UTC) | Purpose |
| --- | --- | --- | --- |
| `bi5/XAUUSD/2024/03/<dd>/XAUUSD_<date>_<HH>h_ticks.bi5` | native hourly LZMA files, one per hour with ticks, in the layout `xq fetch dukascopy` writes | 2024-03-06 to 2024-03-13 | US DST start (Sun 10 Mar) and the weekend gap: no file from Fri 22:00 to Sun 22:00 UTC |
| `csv/XAUUSD_ticks_2024-10-30_2024-11-06.csv` | dukascopy-node tick CSV (`timestamp,askPrice,bidPrice,askVolume,bidVolume`, Unix ms) | 2024-10-30 to 2024-11-06 | US DST end (Sun 3 Nov) and the weekend gap |

Ticks exist only while the synthetic market quotes (the model of the MT5 fixtures): not from
Friday 17:00 to Sunday 18:00 New York, and not from 17:00 to 18:00 New York each day. The files'
hours never move with DST; the market's gaps move by an hour in UTC when New York changes.

Regenerate (the suite checks the `.bi5` files' decoded records and the CSV's bytes against the
generator):

```bash
PYTHONPATH=tests uv run python -m helpers.dukascopy_fixtures
```
