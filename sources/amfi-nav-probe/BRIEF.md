# BRIEF — amfi-nav-probe

> **This source is a test harness, not a Ring 1 source.** `/foundation` stage 3 calls
> for a deliberately throwaway adapter to prove the loop end to end: a plain public
> file, no browser, no session, no pagination. Do not invest in it, do not build on
> it, and do not let it become the de facto AMFI source by accident — when NAVs matter
> for real, run `/new-source amfi-nav` and let the pipeline produce a proper brief.
>
> Written by hand during foundation. A real brief comes from `source-scout`.

## What exists

One URL, one file, no browser required:

```
https://www.amfiindia.com/spages/NAVAll.txt
  -> 302 -> https://portal.amfiindia.com/spages/NAVAll.txt
```

Plain text, semicolon-delimited, ~12k data rows, ~1.5 MB. Published once per business
day, in the evening IST, carrying that day's NAVs. Note the redirect to `portal.` —
the capture records the *resolved* URL in `source_url`, not the requested one, so an
observation's provenance points at where the bytes actually came from.

## Shape of the file

A header line, then repeating blocks of: a scheme-category heading, a fund-house name,
then data rows. Category and fund-house lines contain no semicolons, which is the only
reliable way to tell them from data. Blank-ish separator lines carry a single space.

Verified against a live capture on 2026-08-22 (1.5 MB, ~12k rows):

```
Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Debt Scheme - Banking and PSU Fund)

Aditya Birla Sun Life Mutual Fund

119551;INF209KA12Z1;INF209KA13Z9;Aditya Birla Sun Life Banking & PSU Debt Fund;Direct Plan;IDCW-Re-investment;106.8821;21-Aug-2026
```

| # | Field | Notes |
|---|---|---|
| 0 | Scheme Code | AMFI's own numeric id. Stable. Useful as an alias, not as the key. |
| 1 | ISIN Div Payout/ ISIN Growth | The ISIN. **This is the entity key.** May be `-` or blank. Note the space after the slash in the header. |
| 2 | ISIN Div Reinvestment | A second ISIN for the reinvestment variant. Often `-`. |
| 3 | Scheme Name | Display name. Never join on it. |
| 4 | Plan | `Direct Plan` / `Regular Plan`. |
| 5 | Option | `Growth`, `IDCW-Payout`, `IDCW-Re-investment`, ... |
| 6 | Net Asset Value | Decimal. Occasionally `N.A.` for a scheme that did not price. |
| 7 | Date | `dd-MMM-yyyy`, IST. **This is `observed_at`, not `captured_at`.** |

> **The column count is not stable across time.** An older layout of this file had six
> columns, with no `Plan` and no `Option`, and NAV/Date at indices 4 and 5. A fixed
> positional parse against the old layout reads `Plan` as the NAV, fails to parse it,
> and silently drops **every row** — which is exactly the silent failure CLAUDE.md
> rule 4 is about. **Parse by header name, not by position**, and let the canary's
> row-count assertion catch the day the header itself changes.

## Gotchas that matter

All of these were confirmed against a live 1.5 MB capture on 2026-08-22.

- **The file is not a daily snapshot. It is a last-known-NAV table for every scheme
  that has ever existed.** Of ~13,278 priced rows, only ~7,500 carry the current
  business day; the rest are dormant or closed schemes whose final NAV is stamped
  anywhere from 2012 onwards. Any check that reasons about "the file's date" must use
  the **modal** row date, not the maximum and not the minimum. Any downstream query
  for "today's NAVs" must filter on `observed_at`, not on the capture date.
- **Some rows are forward-dated by a day or two.** The 2026-08-22 capture carried 305
  rows dated 2026-08-23, a Sunday. Do not treat a future or weekend row date as
  corruption; treat a *modal* date that is stale as breakage.
- **NAVs in the millions are real.** The IL&FS Infrastructure Debt Fund series prices
  above 25 lakh per unit because its units were issued at a high face value. A sanity
  ceiling of 1,000,000 rejects legitimate data; 10,000,000 is the working figure.
- **`N.A.` NAVs are real and frequent.** A scheme in the file with no NAV is not an
  error; it is a scheme that did not price that day. Skip the row, do not zero it.
- **Blank or `-` ISIN.** Some schemes carry no ISIN, and the reinvestment column is
  `-` more often than not. Rows with no payout/growth ISIN are skipped rather than
  guessed at — an ORG: key here would be a name join in disguise.
- **Two ISINs per row.** Payout/growth and reinvestment are different instruments with
  the same NAV. The probe records the first only; a real adapter should decide
  deliberately and say so here.
- The file has no pagination, no documented rate limit, and no history endpoint that
  this brief has verified — so a missed day is likely a **permanent** gap.

## Canary expectations (for `canary-author`, from this brief alone)

- Priced-row count is five figures — ~13,000. A drop below ~5,000 means the file was
  truncated or the parse is dropping rows.
- The **modal** row date is within a few days of the collection date, and a healthy
  majority (thousands) of rows carry it. A modal date that drifts backwards is the
  signal that AMFI stopped publishing or that collection is running against the wrong
  date.
- Every NAV is a positive number below 10,000,000.
- Every entity key is a syntactically valid ISIN.
- No `(ISIN, date)` appears twice with different values in one capture.

## What a real version would add

Fund-entity seeding (scheme code and name aliases into the entity graph), the
reinvestment ISIN, scheme category as an observation, and a compliance review against
AMFI's actual terms of use.
