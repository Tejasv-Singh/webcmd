# Entity seeds

**No ISIN in this repo is invented.** Fabricating an identifier to make a seed file
look complete is the single fastest way to poison the corpus, because a wrong ISIN
joins silently and forever. Everything here is either fetched from the issuer or
explicitly marked as a test fixture.

## Seeding the real universe

The authoritative, free list of NSE-listed securities with ISINs is the exchange's own
`EQUITY_L.csv`:

```bash
kosh entities seed-nse            # fetch + parse + load, ~2000 equities with ISINs
kosh entities stats
```

That is the Nifty 500 and then some. Index membership itself (which of those 2000 are
in the Nifty 500 today) is a separate, *versioned* fact — index constituents change,
and a static list of them would be exactly the kind of silently-stale join this graph
exists to prevent. Track it as observations under `index.membership` when a source for
it lands, not as a seed file.

## Mutual funds

AMFI's `NAVAll.txt` carries scheme codes *and* ISINs, so the AMFI probe seeds fund
entities as a side effect of collection:

```bash
kosh collect --source amfi-nav-probe --date today
```

## Files

- `universe.sample.csv` — four **fake** entities (`INE000TEST0*`) used by tests and by
  `kosh verify-foundation`. Never load it into a real store.
