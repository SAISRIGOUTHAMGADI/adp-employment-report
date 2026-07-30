# ADP National Employment Report: tracker and forecaster

A command-line tool that tracks the monthly [ADP National Employment
Report](https://adpemploymentreport.com/) and forecasts the next print.

**Headline finding:** across 39 vintage-correct forecast origins, this model is
**statistically indistinguishable from a three-month moving average** (Diebold-Mariano,
p = 0.882). Most of the engineering here exists to make that claim *checkable* rather than
asserted: point-in-time storage so a backtest cannot read the future, structural guards
against the arithmetic that would fake a good score, and a paired significance test that
would have flagged a real difference had one existed.

That result is the deliverable, not a shortfall. On a series where most month-to-month
movement is genuinely unpredictable, knowing you have not beaten a simple average, and
being able to prove it, is worth more than a number that cannot be defended.

**Status:** complete. One CLI, four subcommands, 445 tests.

---

## Quickstart

Requires Python 3.11+. [`uv`](https://docs.astral.sh/uv/) is used below; `python -m venv`
and `pip` work identically.

```bash
git clone https://github.com/SAISRIGOUTHAMGADI/adp-employment-report.git
cd adp-employment-report
uv venv .venv
source .venv/bin/activate
uv pip install -e '.[dev]'
```

Activating matters: `adp-forecast` is installed into `.venv/bin`, so without it the
shell reports `command not found`. On Windows the activate step is
`.venv\Scripts\activate`. If you would rather not activate, every command below also
works as `.venv/bin/python -m adp_forecast <command>`.

Get a free FRED API key at <https://fredaccount.stlouisfed.org/apikeys>, then:

```bash
cp .env.example .env
# edit .env and set FRED_API_KEY=<your 32-char key>
```

Then ingest, forecast, and inspect:

```bash
adp-forecast ingest                  # pull all 7 series with full revision history (~2s)
adp-forecast history                 # recent published prints
adp-forecast forecast                # next month's prediction, with reasoning
adp-forecast backtest                # accuracy vs naive baselines
```

`adp-forecast --help` lists everything; each subcommand has its own `--help`.

```
$ adp-forecast history -n 4

ADP private payrolls — last 4 observations (thousands of persons)
reference              level    MoM change
------------------------------------------
2026-03-01           132,397          +61k
2026-04-01           132,502         +105k
2026-05-01           132,624         +122k
2026-06-01           132,722          +98k
```

```
$ adp-forecast forecast

ADP is forecast to report a gain of 53,000 jobs for July 2026, published in the
next National Employment Report.
An 80% range runs from a loss of 26,000 jobs to a gain of 101,000 jobs.

The last 6 prints averaged a gain of 77,000 jobs; this forecast sits below that.
Made from data available on 2026-07-30.

Why:
  Start from the average month in the 155 months the model was fitted on
  (a gain of 155,000 jobs), then adjust for current conditions:
  ADP average change over the past year is 67,333, which subtracts 52,000 jobs.
  ADP change last month is 98,000, which subtracts 38,000 jobs.
  Initial claims level this month is 202,750, which subtracts 17,000 jobs.

That is 45,000 jobs below last month's print of a gain of 98,000 jobs.

Caveats:
  - Drivers show statistical association fitted on 155 months, not causation.
  - The range comes from the spread of past backtest errors, which assumes error
    dispersion is stable over time. Measurement shows it is not, so the range is
    approximate.
  - March 2020 to June 2022 is excluded from training. Those months are real
    history but not repeatable dynamics.
  - Some inputs lag the forecast month by design: BLS private payrolls (1 month
    behind), Job openings (JOLTS) (2 months behind).
```

`--with-accuracy` makes the caveats quote measured backtest figures. `--json` emits the
same forecast as a machine-readable payload, the shape an HTTP endpoint would return,
available only because the service layer returns dataclasses rather than strings.

`ingest` is idempotent: re-running upserts on the vintage key and closes any window a
revision has superseded. There is deliberately no incremental mode: a full re-ingest
costs ~2s, and a cutoff would miss a revision to an older observation arriving after it.

### Tests and linting

```bash
pytest                      # everything (445 tests)
pytest -m "not live"        # offline only, no API key needed
flake8 src tests scripts
```

Unit tests never touch the network. The integration tests are marked `live` and skip
automatically when `FRED_API_KEY` is unset, so a fresh clone with no credentials still
runs green.

---

## How this was built: who decided what

I used AI heavily, as the brief asked. That makes the division of labour a fair question,
so here it is plainly, without requiring anyone to read a 1,100-record session log.

**The short version.** I set the architecture and made every judgment call that shaped the
result. The AI did the research, wrote the code to my constraints, and was overruled
whenever its instinct conflicted with mine. The most consequential decisions in this
project were ones where I told it no.

### Decisions I made, and can defend

| Decision | What I chose, and why |
|---|---|
| **Overall architecture** | Ports and adapters across six layers: ingestion, storage, features, forecast, evaluation, explanation. I specified this in the first message, before any code existed, and it survived unchanged to the end. |
| **Vintage-aware storage** | I chose to store the full revision history rather than current values. It is the difference between a backtest that measures forecasting skill and one that measures hindsight. This one decision drives the whole schema. |
| **Exclude COVID, never winsorise** | Clipping the 2020 to 2022 outliers to a plausible bound would fabricate observations that never happened and present them as data. Excluded the window instead, with the boundary chosen from measured volatility rather than intuition. |
| **Calendar-month mean over the BLS reference week** | Claims are jumpy week to week and a single week gets distorted by holidays. Averaging the month uses every observation, which is the same reasoning behind the four-week moving average everyone reads claims through. The reference week is conceptually neater but noisier, and we are predicting the move, not rebuilding the official number. |
| **A structural guard, not a feature flag** | The AI offered to keep rebenchmark masking behind a default-off flag. I rejected it: that is dead scaffolding for a problem that no longer exists. I asked for the real rule enforced instead, so cross-vintage arithmetic now raises rather than being merely discouraged. |
| **One unit conversion, enforced by test** | ADP publishes persons, BLS publishes thousands. I would not accept every reader dividing by 1000 on its own, because someone forgets or someone does it twice and the forecast is wrong by 1000x with nothing throwing an error. One function, one test guarding it. |
| **Stop chasing accuracy** | When the model reached parity with a three-month mean, I called it. Continuing would have meant tuning against the test set, and the honest number plus the diagnosis was always the deliverable. |
| **Prove the tie** | I asked whether ridge's 62.1 versus 63.4 was real or luck. That question produced the Diebold-Mariano test and the p = 0.882 result that the README now leads with. |
| **Engineering standards** | SDLC discipline, code to interfaces, complexity awareness, Flake8 clean, docstrings, real logging, a full exception hierarchy, and tests on everything. Set as non-negotiable before the first line was written. |

### Where I overruled it

These are the moments that most define the result:

- It proposed **rebenchmark masking behind a flag**. I refused the flag and demanded the
  invariant be enforced structurally. The change computation now refuses to subtract across
  vintages at all.
- It started a **nine-agent research fan-out** for what should have been a handful of API
  calls. I killed it and set a standing rule: ask before spawning work.
- It began **running my clean-clone verification for me** after I said I would do it
  myself. I stopped it, and doing it myself is what found the two release-blocking bugs
  below.
- It wanted to add **exponential sample weighting** after the bias fix. I had it justify
  that on a diagnosed defect rather than on hope, and when it could not, we dropped it.

### Bugs I found by testing the documented path

I re-cloned the repository from scratch and followed the README line by line, which the AI
had never done because it always worked from a configured tree:

1. **`adp-forecast: command not found`.** The README never said
   `source .venv/bin/activate`. That is the wall a reviewer hits thirty seconds in.
2. **A stale command in the program's own output.** The accuracy caveat still pointed at
   `scripts/backtest.py` after the CLI migration made that a deprecated shim.

### Where the AI genuinely earned its place

Being fair in the other direction, it corrected me on real facts and caught its own errors:

- **My API endpoint was wrong.** I specified `api.fred.stlouisfed.org`, which does not
  exist. It found the correct host by probing rather than trusting me.
- **A units mismatch I would have missed.** ADP publishes persons where BLS publishes
  thousands, a 1000x error that produces plausible-looking output.
- **It reversed its own cost estimate by four orders of magnitude** after measuring instead
  of reasoning, which is what made full vintage history affordable at 14 requests.
- **It reported that its own model lost to a three-month mean** rather than quietly tuning
  until it did not, and it caught two invalid measurements in its own analysis: comparing
  models scored over different origin subsets, and a coverage figure computed against the
  wrong denominator.

**Everything above is verifiable.** [`PROMPTS.md`](PROMPTS.md) indexes the session
turn by turn, and [`prompts/session-transcript.jsonl`](prompts/session-transcript.jsonl)
is the raw log if you want to check any claim against what actually happened.

---

## Data: what this tracks and why

Everything below was verified against the live FRED API on 2026-07-30 rather than
assumed. The series registry in
[`src/adp_forecast/config.py`](src/adp_forecast/config.py) is the single source of
truth; no series ID is hardcoded anywhere else.

| Series | Role | Freq | Lag | Units | Why |
|---|---|---|---|---|---|
| `ADPMNUSNERSA` | **target** | Monthly | 1 | Persons | ADP total private payroll level. Its MoM change is the headline. |
| `ICSA` | feature | Weekly | 0 | Number | Initial jobless claims, the *flow into* unemployment. Most timely labour signal. |
| `CCSA` | feature | Weekly | 0 | Number | Continued claims, the *stock* staying unemployed. Confirms blip vs. trend. |
| `USPRIV` | feature | Monthly | 1 | Thous. | BLS private payrolls, the correct official comparator for ADP. |
| `PAYEMS` | feature | Monthly | 1 | Thous. | Total nonfarm. Carried so `PAYEMS − USPRIV` yields government payrolls free. |
| `UNRATE` | feature | Monthly | 1 | Percent | Unemployment rate. Coincident not leading; retained as a level check. |
| `JTSJOL` | feature | Monthly | **2** | Thous. | JOLTS job openings, meaning labour demand. Published a month later than everything else. |

### What is forecast, and what is not

The brief asks for "the next set of numbers". ADP publishes more than one, so this is a
scope decision rather than an oversight. Counting what is actually in the release
(`release_id=194`, verified live):

| What ADP publishes | Series in the release | Forecast here |
|---|---|---|
| Total private employment (**the headline**) | 5 | **Yes**, `ADPMNUSNERSA`, monthly SA |
| By industry | 68 | No |
| By census division | 36 | No |
| By establishment size | 20 | No |
| Pay growth (job-stayers vs job-changers) | **0, not carried on FRED** | No |

**Why the headline only.** It is the number the report is known by, the one wire services
lead with, and the one every consensus forecast is quoted against, so it is the only
figure with an external benchmark to be judged by. The 124 breakdown series are the same
modelling problem repeated with thinner data per cut; forecasting them would multiply
runtime and surface area without demonstrating anything the headline does not.

Pay growth is a genuinely different target, and it is **not available on FRED at all**.
Ingesting it would mean scraping the ADP site, a second adapter and a second data
contract. `IngestionPort` is the seam that would make that additive rather than invasive,
but it was out of scope for this build.

The architecture does not hard-code this choice. `SeriesRole.TARGET` is a registry
attribute, so pointing the model at an industry cut is a registry edit, not a code change.

### Data facts that drive the design

Each of these was a wrong or missing assumption at the start of the project, caught by
probing the API before writing code:

1. **The API host is `api.stlouisfed.org/fred`.** There is no `api.fred.stlouisfed.org`.
2. **`NPPTTL` is discontinued.** Its FRED title literally ends `(DISCONTINUED)` and the
   final observation is `2022-05-01`. It was replaced when ADP and the Stanford Digital
   Economy Lab
   [changed methodology in August 2022](https://mediacenter.adp.com/2022-08-23-ADP-Research-Institute-and-Stanford-Digital-Economy-Lab-Unveil-New-Methodology-for-ADP-National-Employment-Report).
3. **ADP publishes `Persons`; BLS publishes `Thousands of Persons`.** ADP's June 2026
   level is `132,722,000` where `USPRIV` is `135,613`. Units are normalised at the
   ingestion edge via `SeriesSpec.scale_to_thousands`, because a 1000× error that
   reaches the model is nearly invisible in output.
4. **`ADPMNUSNERSA` is revised, and 47 vintages exist.** Not monthly, though: ADP revises
   *once a year* at the January QCEW rebenchmark. The January 2026 rebenchmark moved
   the entire historical level down ~2.4 million.
5. **Errors are always HTTP 400 with a JSON body**, for a bad series ID *and* for a
   rejected key. FRED never returns 5xx for bad input, which is why 4xx is never
   retried.
6. **Page limits are per-endpoint.** `series/observations` accepts `limit=100000`;
   `release/dates` rejects anything above `10000` with an HTTP 400 rather than clamping.
7. **FRED returns scheduled *future* release dates.** Requesting ADP release dates from
   2024 currently returns dates through December 2026.
8. **An unknown `release_id` returns HTTP 200 and an empty list**, so a typo is
   indistinguishable from "no releases" by status code. Logged as a warning.
9. **Missing values arrive as the string `"."`**, present in `ICSA` (2) and `UNRATE` (1)
   within the tracked window, so this is live behaviour, not a theoretical case.

---

## Approach

### Architecture

Ports and adapters. Each layer depends on the *contract* above it, never on a concrete
implementation:

```
                 ┌──────────────────────────────┐
   CLI / API ───► │  service layer (typed objs)  │
                 └──────────────┬───────────────┘
      ┌─────────────┬───────────┴────┬──────────────┐
      ▼             ▼                ▼              ▼
  forecast      features         evaluation     explanation
      └─────────────┴────────────────┴──────────────┘
                          ▼
                    StoragePort           (SQLite adapter)
                          ▼
                   IngestionPort          (FredAdapter)
                          ▼
                    FRED REST API
```

Two decisions worth naming:

- **`IngestionPort` and `ReleaseCalendarPort` are separate protocols.** Any source can
  hand back a time series; only a source with a publication calendar can say *when*
  each value was released. One combined interface would force a CSV-backed adapter to
  stub a method it cannot honour.
- **Protocols (structural typing), not abstract base classes.** An adapter needs no
  import from `port.py` to conform, so the dependency arrow points one way and test
  doubles stay trivial.

The service layer will return typed dataclasses, never formatted strings. That is what
keeps a FastAPI shim a ~40-line addition instead of a rewrite, and it makes the
explanation layer assertable on structured `reasons` rather than on prose.

### The vintage model: the central design decision

An observation is keyed by **three** dimensions, not two:

```
(series_id, reference_date, realtime_start)
```

`reference_date` is the period the number describes. `realtime_start`/`realtime_end`
are the window during which that value was the published truth. A statistical agency
revising a number does not overwrite history: it closes one window and opens another.

This is not academic. `USPRIV` for April 2026 has three vintages:

```
135,428   known 2026-05-08 .. 2026-06-04
135,494   known 2026-06-05 .. 2026-07-01
135,467   known 2026-07-02 .. now
```

Storing the window lets a backtest ask *"what did I know on 2026-05-20?"* and get an
honest answer. `Observation.known_on(as_of)` is a one-line filter that reconstructs
the dataset exactly as it existed that day. Without it, a backtest scores forecasts
using numbers that had not been published yet, and reports a flattering, meaningless
accuracy figure.

For the ADP target itself, the size of the problem is stark. Derived month-over-month
changes, first print versus today's value:

| Month | As published | Today | Difference |
|---|---|---|---|
| 2025-03 | +285k | −53k | −338k |
| 2025-09 | −91k | +88k | +179k |
| 2025-11 | −27k | +74k | +101k |

September 2025 was published as **−91k** and now reads **+88k**. With typical prints in
the ±100k range, **the revision is larger than the signal.** Scoring against the current
vintage would not measure forecasting skill; it would measure whether you guessed a
future rebenchmark.

### Key tradeoffs

**Full revision history over per-origin snapshots.** The first instinct was to snapshot
the dataset once per forecast date: ~198 origins × 7 series = 1,386 requests. Measuring
proved that wrong. FRED range-compresses unchanged vintages, so rows scale with the
number of *edits*, not observations × vintages, at a measured 1.85 to 9.93 rows per
observation. The complete revision history for all seven series is **14 requests and
18,224 rows (1.8 MB)**, and it is a strict superset: any snapshot is recoverable as a
`WHERE realtime_start <= :as_of AND realtime_end >= :as_of` filter. Cheaper *and* more
informative than the alternative.

**Model the change, not the level.** The January rebenchmark shifts the level by
millions. A level model trained across that discontinuity fits an accounting artifact.
Changes are stationary across rebenchmark levels.

**No rebenchmark masking, a structural guard instead.** An earlier plan masked the ~14
January transition months. Measurement killed it. A rebenchmark restates the *entire*
history at once, so any single snapshot is internally consistent and a change computed
inside one is correct, January included. Across all 46 buildable panels, every January
is plausible (+106k, +107k, +183k, +22k). The corruption appears only when differencing
*across* two vintages:

| Reference month | Cross-vintage diff | True published change |
|---|---|---|
| 2023-01 | +4,616k | +106k |
| 2024-01 | +1,926k | +107k |
| 2026-01 | −2,307k | +22k |

Masking discarded 14 real observations to avoid a mistake nobody should make, and left
the mistake possible everywhere else. So the rule is enforced structurally instead:
[`changes.py`](src/adp_forecast/features/changes.py) requires an explicit `as_of`, and
refuses any subtraction whose operands were not jointly published on it, raising
`VintageMismatchError` rather than returning a number that never existed. Same principle
as the units choke point: make the highest-impact bug impossible to reintroduce quietly,
rather than adding a flag nobody will enable.

**Weekly→monthly by calendar-month mean.** Claims are jumpy week to week and any single
week is vulnerable to a holiday or one-off spike, so averaging every week in the month
uses all the information and gives a steadier signal. That is the same reasoning behind the
four-week moving average that is the standard way claims are read. The alternative, the
BLS reference week containing the 12th, matches how payrolls are measured on paper but
keeps one week and discards the rest; we are predicting the *move*, not reconstructing
the official number. Both are fully published before the ADP release, so leakage favours
neither. Built as a swappable rule (`AggregationMethod` enum + registry) so the
reference-week variant is one function if the backtest shows claims matter.

A **mean** rather than a sum matters more than it looks: 138 stored months contain 4
week-ending Saturdays and 73 contain 5, so a sum would inject a spurious 25% swing from
calendar drift alone. A month with fewer than 2 contributing weeks is reported missing
rather than guessed, because the month in progress is usually partial at forecast time.

**The one-day rule.** The forecast origin for a print released on date `R` is `R − 1
day`. Two independent reasons: the snapshot at `R` already contains the reference month
released that morning, the answer itself, and other series publish the same morning,
some after ADP's 08:15 ET release. Verified: at origin `2026-06-30`, June is invisible
and May is the newest known.

**Publication lags need no manual shifting.** Because every series is read at the same
`as_of`, a series in arrears is simply absent for its missing months. JOLTS resolves to
T−2 with no lag arithmetic anywhere in the code.

**SQLite over CSV.** Not chosen for scale (~18k rows is trivial either way) but for the
key structure. A three-part vintage key with idempotent re-ingest is
`INSERT ... ON CONFLICT DO UPDATE`; in CSV it is read-all-into-pandas, dedupe, rewrite.
`sqlite3` is stdlib, so it adds zero dependencies to a clone-and-run.

**One unit-conversion choke point, enforced by test.** `to_thousands()` in
[`units.py`](src/adp_forecast/units.py) is the only code that reads
`scale_to_thousands`. A 1000× error throws no exception and produces plausible-looking
output, so relying on every reader to remember the conversion, and to apply it exactly
once, is not a control. `tests/test_units.py` asserts across the whole source tree that
no other module references the scale factor or hand-rolls a `/ 1000`, so a second
conversion site fails the build rather than shipping.

**No secondary index on the realtime columns.** A
`(series_id, realtime_start, realtime_end)` index was added, measured, and removed.
Because `observations` is `WITHOUT ROWID`, the primary key *is* the table, ordered
`(series_id, obs_date, realtime_start)`, so a `series_id` seek yields a contiguous run
already sorted in exactly the order the point-in-time query wants. SQLite never chose
the index even with `ANALYZE` statistics present. Over 6,160 as-of queries the runtime
was identical (4487ms vs 4488ms) while the index cost 784 KB, a third of total database
size, plus write amplification on every ingest. Documented in
[`schema.sql`](src/adp_forecast/storage/schema.sql) with the condition under which to
revisit.

**Storage rejects display-only records.** `fetch(all_vintages=False)` reports every
row's `realtime_start` as the *fetch* date rather than the real publication date, and
since both it and genuine current-vintage data carry
`realtime_end = '9999-12-31'`, the schema cannot express the difference. Persisting them
would look identical to real history while silently destroying point-in-time
reconstruction. `upsert_observations` therefore enforces the one invariant that does
separate them: a genuine batch contains at least one row whose `realtime_start` predates
its own `fetched_at`.

**Retry on our own exception types, not on HTTP status codes.** `retry.py` retries
`TransientIngestionError` and re-raises `PermanentIngestionError` immediately. This
keeps the retry policy source-agnostic, since a future database adapter reuses it unchanged
by classifying its own failures into the same split, and it means a typo'd series ID
costs one request instead of four against a ~120 req/min budget.

**Known limitation:** ADP vintages only extend back 47 months, because ALFRED holds no
as-of data for the series before the 2022 methodology change. A *fully* vintage-correct
backtest is therefore only possible over ~47 origins. This is a hard data limit, not a
storage choice. The evaluation plan below accounts for it with two scorecards.

---

## How forecast accuracy was evaluated, and what the results were

Reproduce everything below with:

```bash
adp-forecast backtest
```

**Protocol.** Expanding-window walk-forward. Every model is refit from scratch at each
origin and asked for one month ahead. Origins are *real ADP release dates* pulled from
FRED (`release_id=194`), never a computed "first Wednesday" rule, which drifts around
holidays, and an origin one day late leaks data that did not exist yet, producing no
error and an implausibly good score. Scheduled future dates are excluded.

**Models are scored only on origins where every model produced a forecast.** They have
different data requirements. Ridge needs a 12-month trailing window the earliest origins
cannot supply, so scoring each on whatever it managed would compare them over different
months and different difficulty. Dropped origins are reported, not absorbed.

### Headline: vintage-correct scorecard

39 origins, Feb 2023 → Jul 2026. Point-in-time panels; each forecast scored against the
number ADP **actually printed that morning**, not today's revised figure.

| model | n | MAE | RMSE | bias | dir% | cover | gap | width |
|---|---|---|---|---|---|---|---|---|
| **ridge** | 39 | **62.1** | 88.0 | +3.0 | 95% | 85% | +5pp | 256k |
| random_walk | 39 | 66.3 | **84.1** | +5.4 | 92% | 97% | +17pp | 376k |
| mean_3m | 39 | 63.4 | 84.6 | +7.6 | 95% | 92% | +12pp | 319k |
| mean_6m | 39 | 66.9 | 88.1 | +15.5 | 95% | 95% | +15pp | 309k |
| drift | 39 | 67.1 | 84.7 | +7.4 | 92% | 97% | +17pp | 382k |

**Ridge has the best MAE, and that means nothing.** It beats the random walk by 6.3%
and the 3-month mean by 2.0%, but a ranking is not a result until it survives a test.

#### Why Diebold-Mariano

The test compares two forecasters on the same data. Rather than comparing two MAE figures
as if they were independent samples, it works on the **loss differential**. For each
month that is `loss(model error) - loss(baseline error)`, and the test asks whether its mean is
distinguishable from zero.

That framing matters because the comparison is **paired**. Both models forecast the same
39 months from the same inputs, so when March is a hard month both miss it. Treating the
two MAEs as independent throws away that correlation, which is exactly the information
that makes the test able to detect a small but *consistent* edge. A model that beat the
baseline by 1.3k every single month would be significant here; ridge doesn't, because its
per-month differential swings wildly around that average.

**Loss is a parameter, not a constant.** MAE and RMSE can rank models differently, and
in the table above they do. Testing under absolute loss speaks to the MAE ranking, under
squared loss to RMSE. Reporting only one would hide the disagreement.

#### Why both Diebold-Mariano and Harvey-Leybourne-Newbold

These are not two competing tests. **HLN is a correction applied to the DM statistic**, and
this project uses both together, which is standard practice.

**Diebold-Mariano (1995)** provides the statistic. It asks whether the mean loss
differential is distinguishable from zero:

```
gamma0 = (1/T) * sum((d - dbar)^2)        lag-zero autocovariance
DM     = dbar / sqrt(gamma0 / T)
```

**Harvey, Leybourne and Newbold (1997)** showed that DM over-rejects on short samples, so
in practice the raw 1995 statistic is rarely used on its own. Their correction rescales it
and swaps the reference distribution from standard normal to Student's *t*:

```
S1* = DM * sqrt((T + 1 - 2h + h(h-1)/T) / T)      compared against t with T-1 df
```

With T = 39 that matters. Uncorrected, the test would reject too readily and manufacture
exactly the false confidence it exists to prevent.

**Only h = 1 is implemented, deliberately.** At h = 1 the `h(h-1)` term is exactly zero, so
the factor reduces to `sqrt((T-1)/T)` with no approximation. Multi-step horizons need more
than this correction: the loss differential becomes autocorrelated, so `gamma0` would have
to be replaced by a HAC (Newey-West) long-run variance. Implementing the general HLN factor
while keeping a plain `gamma0` would look like multi-step support without being it, so
`diebold_mariano` raises on `horizon > 1` instead.

One subtlety worth recording, because it was a real bug here: DM specifies the **biased**
`1/T` divisor for `gamma0`. The unbiased `1/(T-1)` sample variance already contains a
`sqrt((T-1)/T)` factor, which at h = 1 *is* the HLN correction, so using it applies the
correction twice and shrinks the statistic by about 1.3% at this sample size. The first
implementation here did exactly that.
[`tests/test_significance.py`](tests/test_significance.py) now pins the statistic against
the published formulae written out longhand, at four sample sizes.

**One assumption worth stating:** the loss differential must be serially uncorrelated,
which holds for one-step-ahead forecasts and is all this project produces. Longer horizons
would need a HAC variance estimator, so `diebold_mariano` **raises on `horizon > 1`**
rather than quietly returning an overconfident number.

*t* is implemented in
[`significance.py`](src/adp_forecast/evaluation/significance.py) via the regularised
incomplete beta rather than imported, because scipy is a ~40 MB dependency for one function
in a project that otherwise installs in seconds. It is validated against published critical
values at seven degrees of freedom, including the df = 38 these 39 origins produce.

#### Results

Diebold-Mariano, paired on the same origins, one-step-ahead, with the
Harvey-Leybourne-Newbold small-sample correction:

| ridge vs | loss | mean diff | t | p | verdict |
|---|---|---|---|---|---|
| random_walk | absolute | -4.2 | -0.50 | 0.620 | indistinguishable |
| random_walk | squared | +674.4 | 0.39 | 0.701 | indistinguishable |
| **mean_3m** | **absolute** | **-1.3** | **-0.15** | **0.882** | **indistinguishable** |
| mean_3m | squared | +578.2 | 0.28 | 0.780 | indistinguishable |
| mean_6m | absolute | -4.8 | -0.59 | 0.562 | indistinguishable |
| drift | absolute | -5.0 | -0.61 | 0.547 | indistinguishable |

**Eight comparisons, no multiplicity correction, and it does not matter here.** Four
rivals under two loss functions is eight tests; at alpha = 0.05 that carries roughly a 34%
chance of one spurious rejection if every null were true. No Holm or Bonferroni adjustment
is applied, because **none of the eight is significant** and a family-wise correction only
ever makes rejection harder. It would matter if a future run produced a lone significant
result, and the module documents that.

**Not one comparison is significant.** At p = 0.882, if ridge and a 3-month mean were
genuinely identical forecasters you would see a gap this large or larger 88% of the time
purely from which 39 months you happened to land on.

The result cuts both ways, which is what makes it credible rather than convenient:
ridge's apparently *worse* RMSE is equally not real (p = 0.701 to 0.780). Neither the win
nor the loss survives contact with a significance test.

So the honest claim is narrow and stated deliberately: **on 39 vintage-correct origins,
this model is statistically indistinguishable from a three-month moving average.**

**What that does not mean.** A non-significant result is not proof the models are equal.
With 39 observations the test has limited power, so it cannot rule out a genuine edge too
small for this sample to see. The correct reading is *"no improvement has been
demonstrated"*, not *"no improvement exists"*. Those are different claims and only the
first is supported.

That distinction is also the argument for stopping. If the measurement cannot resolve a
difference this size, then any further tuning that appears to help is unfalsifiable. We
would be selecting on noise and unable to tell. On a series where most month-to-month
movement is genuinely unpredictable, a simple mean being hard to beat is a finding about
ADP, not a failure of the model.

Every figure above is `adp-forecast backtest` output rather than a claim in prose, and the
test would have flagged a real difference had one existed. A synthetic case with a
consistent edge is asserted to come back significant in
[`tests/test_significance.py`](tests/test_significance.py), so a non-result here is
informative rather than a broken test.

### Secondary: lag-shifted scorecard (approximate)

119 origins, Aug 2013 → Jun 2026. Extends coverage by approximating each origin from
current-vintage data truncated by declared publication lags.

| model | n | MAE | RMSE | bias | cover |
|---|---|---|---|---|---|
| **ridge** | 119 | **48.6** | **65.3** | +9.0 | 71% |
| random_walk | 119 | 56.0 | 77.6 | +3.5 | 78% |
| mean_3m | 119 | 69.0 | 94.4 | +6.3 | 76% |
| mean_6m | 119 | 63.6 | 83.8 | +11.2 | 78% |
| drift | 119 | 56.3 | 78.1 | +5.9 | 79% |

**Read this one sceptically. That is why it is secondary.** It uses *revised* figures
where a real forecaster had first prints, so it cannot measure revision effects. Ridge
scores MAE 48.6 here against 62.1 on the honest scorecard: **the approximation makes the
model look 22% better than it is.** That gap is itself the argument for building
vintage-aware storage in the first place.

### The bias fix, and the line I did not cross

The first honest backtest put ridge at **MAE 67.5 with +18.7k bias**, barely better than
the random walk. Diagnosis: a ridge intercept is the training mean, and 72% of usable
history predates 2020 averaging **+180k**, against **+54k since 2024**. The model was
anchored to a labour market that no longer exists.

The fix was a 12-month trailing-mean term giving it a local anchor, added on that
structural argument, before seeing whether it helped. It did: **bias +18.7k → +3.0k**,
MAE 67.5 → 62.1.

What was **not** done: tune the training window, the exclusion boundary, or the
regularisation by watching backtest error. Choosing a hyperparameter by test-set
performance is the same leak the vintage design exists to prevent, and would make every
number above meaningless. A second candidate change (exponential sample weighting) was
dropped for exactly this reason. Once the diagnosed bias was fixed, it had no
justification left except hoping the score improved.

### Explaining the forecast

The brief's third requirement, *understand why*, imposes a stricter constraint than it
first appears: the prose must be **derived from the model's arithmetic**, not written
alongside it. A narrative composed independently can drift from the numbers it describes
and nobody would notice.

This is the reason ridge was chosen over a stronger black box. A linear model's
prediction decomposes exactly into an intercept plus one `coefficient × feature`
contribution per term, so each sentence is generated from a structured `Driver`, and the
claim that those contributions sum to the reported forecast is **verified rather than
trusted**. `ridge.py` asserts the identity on every call, and the explainer raises
`ExplanationError` if the drivers imply an impossible intercept.

Three things the explanation deliberately will not do:

- **Claim the model is accurate.** Measured accuracy is passed in as a `ScoreCard` and
  the wording is driven by whether the model actually beat its baseline, so a losing
  model describes itself as losing. Nothing is hardcoded, so the prose cannot go stale
  when the backtest changes.
- **Present a driver as causal.** Coefficients are associations fitted on ~155 months.
- **Hide what it doesn't know.** Stale inputs, the excluded regime, and the measured
  interval limitation are all surfaced as caveats.

Output is a structured `Explanation`, never a string: tests assert on fields, the script
renders text, and an HTTP layer could serialise it without reparsing prose.

### Metrics, and one that is deliberately absent

MAE in thousands of jobs is primary, being the same units as the forecast and directly interpretable
against a print that runs around 100k. RMSE is always reported alongside because it can
disagree, and here it does.

**MAPE is excluded on purpose.** The target changes sign and passes near zero. Recent
prints include -1k, +11k and +22k. Percentage error against a near-zero denominator
explodes, so MAPE would rank models by how well they dodged small-actual months.

**Baselines.** Random walk, 3- and 6-month means, drift. *Not* seasonal naive: the series
is already seasonally adjusted, so predicting "same as twelve months ago" would re-apply
a pattern that has been removed. That is a wrong baseline, not a weak one.

### Intervals: a measured, unfixed limitation

Intervals are empirical quantiles of forward-chaining residuals, not model-implied
variance, because payroll errors are not reliably normal. That avoids assuming a *shape*,
but it still assumes error *dispersion* is stable between training history and the
forecast month, and measurement says it is not:

| scorecard | realised error sd | residual pool sd | ratio | coverage vs 80% |
|---|---|---|---|---|
| vintage | 87.9k | 118.3k | 0.74 | 85% (over) |
| lag_shifted | 64.7k | 53.4k | 1.21 | 71% (under) |

Width tracks the residual pool, so the two scorecards miss in **opposite directions**,
which rules out a constant correction factor. A hypothesis that alpha selection was
double-dipping (choosing the penalty on the same folds whose residuals build the
interval) was tested and **refuted**: pinning alpha moved coverage by under one point.

Left uncorrected deliberately. Fitting a residual window or scale factor against backtest
coverage is the same test-set tuning refused above. The headline scorecard errs
conservative at 85% against a nominal 80%, which is the safe direction; the under-covering
case is confined to the approximate scorecard and is reported rather than papered over.

**Still missing:** a published-consensus benchmark. Beating naive baselines shows the
model is not trivial; matching professional consensus MAE would show it is good. That
comparison needs consensus figures FRED does not carry.

---

## Roadmap

- [x] **Ingestion.** `IngestionPort` + `FredAdapter`, vintage-aware, retry
- [x] **Storage.** SQLite, three-part vintage key, idempotent upsert, per-series
      checkpoints, `units.py` conversion choke point
- [x] **Features.** Calendar-month-mean aggregation behind a swappable rule,
      vintage-safe differencing, point-in-time panel assembly
- [x] **Forecast.** Hand-rolled numpy ridge + four naive baselines behind one port
- [x] **Evaluation.** Walk-forward backtest, two scorecards
- [x] **Explanation.** Plain-English "why" generated from the model's own arithmetic,
      with a consistency guard
- [x] **CLI.** One `typer` entry point, four subcommands, `--json` output
      (445 tests total)
- [ ] Optional: FastAPI shim, Cloud Run

### What I'd build next with another week

1. **Weekly ADP nowcast.** The ADP release (`release_id=194`) carries 129 series,
   including `ADPWNUSNERSA`, the same total private payroll target measured *weekly*,
   plus weekly cuts by industry, establishment size and census division. ADP's own
   high-frequency data on the exact target. One caveat to check first: the weekly series
   currently ends `2026-05-16` against the monthly's `2026-06-01`, so it may lag rather
   than lead.
2. **Revision-momentum features.** The stored vintage history already supports this.
   `USPRIV`/`PAYEMS` are revised twice with meaningful magnitude and BLS revisions are
   known to be serially correlated. `ICSA`/`CCSA` revise by a near-mechanical +1k and
   ADP has no intra-year revisions at all, so those carry nothing.
3. **Additional timely indicators**, namely the Indeed Job Postings Index (daily),
   `TEMPHELPS` (temporary help services, classically leading), and average weekly hours.
4. **Vintage-aware feature store** with as-of caching, so a full backtest sweep does not
   re-derive point-in-time features per origin.

---

## AI usage

The full session log is included in two formats:

| File | What it is |
|---|---|
| [`prompts/session-transcript.jsonl`](prompts/session-transcript.jsonl) | The **raw Claude Code log**: every record, byte-for-byte except for a redacted API key. |
| [`prompts/session-transcript.md`](prompts/session-transcript.md) | The same records rendered readable: every prompt, response, tool call and result, in order. |
| [`PROMPTS.md`](PROMPTS.md) | A curated turn-by-turn index: what was asked, what came back, what I did with it. Start here. |

Both exports come from [`tools/export_transcript.py`](tools/export_transcript.py), which
is committed so the transformation is inspectable. The only alteration is secret
redaction: a live FRED API key leaked into an HTTP error message that echoed the request
URL, and every occurrence was replaced. Deliberately-invalid keys used during the session
to probe error handling are preserved, because that probe is part of the story.

Nothing else is removed. The dead ends are all there: an hour lost to a nonexistent API
host, a nine-agent research fan-out that got killed for burning tokens, a cost estimate
wrong by four orders of magnitude, and a model that lost to a three-month moving average
before being diagnosed and fixed.
