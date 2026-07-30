# AI session log

Complete log of AI usage on this project. Not sanitised — the dead ends, the wrong
answers and the corrections are all here, because they are the parts that actually
shaped the design.

**Tool:** Claude Code (Opus 5), Claude Code desktop app on macOS.
**Session 1:** 2026-07-30. Continuous session covering data verification, architecture
decisions, and the ingestion layer.

A note on how this log is structured: this was one long interactive session, not a
series of one-shot prompts. Each turn below is my prompt verbatim (lightly trimmed only
where I pasted the same six FRED URLs a second time), followed by what the model did and
what I did with the output.

---

## Turn 1 — Project framing, plan review, and "challenge me on the data"

**My prompt** (abridged only where it quotes the assignment brief verbatim):

> I have a take home assignment. [pasted the full assignment brief]
>
> So based on that, I already created a repo in my own personal github, put that as
> public for now. Cloned it locally --/Users/goutham.gadi/repos/adp-employment-report
> So that I can commit there and push it. I do not know how to export the claude chat so
> keep in mind that this chat in this has to go there okay? Please follow the pattern
> that they requested.
>
> My thought process was this
>
> 1. you need 1 ingestion layer which is grabbing stuff from ADP using the fred API
> 2. Next you gotta store that in a DB instances or something, can we use sql lite? I do
>    not know how many rows and stuff are gonna be there, or using csv is better? Why sql
>    lite is its more easier for them to clone and run it cause the python libraries
>    support it easily. (Let me know your thoughts on this) View this is as a system
>    design problem and let me know.
> 3. Then you build a forecast layer like basically it produces best-guess for next month
>    plus a realistic high-log range or something.
> 4. The most important thing is the WHY, why did you preidct that ? why did you do that?
>    Why did you forecast that? You gotta just convert that in to plain english as per the
>    requirement.
> 5. Backtest/EVAL engine --> To measure how accurate our past forecasts are.
> 6. CLI typer interface --> commands a person types to use all of the stuff above.
> 7. I was thinking might as well just wrap this on a fast api thing and deploy it in a
>    cloud run ---> talk to me on that [...] "A web UI is optional." So we will see, when
>    building we gotta make it easier to do both.
>
> Before going down and building the ingestion layer below, tell me how my thought
> process fits to what the requirements have been asked and did I miss any make a full
> blown assessment and tell me first. [...]
>
> Build ONLY the ingestion layer right now. No forecasting yet.
>
> Data facts you need (don't guess these, CHALLENGE ME - do a comprehensive research and
> challenge me):
> - Source is FRED (Federal Reserve Economic Data) via its REST API.
> - Target series: ADPMNUSNERSA (ADP total private payroll employment, level, SA,
>   monthly). Do NOT use NPPTTL — it's discontinued since 2022.
>
> Links that I manually checked and researched :
> https://fred.stlouisfed.org/series/ADPMNUSNERSA
> https://fred.stlouisfed.org/series/NPPTTL
> I saw this press release notes too that confirms the discontinuation
> https://mediacenter.adp.com/2022-08-23-ADP-Research-Institute-and-Stanford-Digital-Economy-Lab-Unveil-New-Methodology-for-ADP-National-Employment-Report
>
> DO NOT TAKE MY WORD FOR IT, YOU HAVE TO RESEARCH and get back to me if I am correct or
> not, cause this is a pure business requirement and I might have made a mistake.
>
> - Also fetch indicators: ICSA, CCSA, PAYEMS, UNRATE, JTSJOL USPRIV [+ FRED URLs]
>
> I might have missed something here, YOU NEED TO DOUBLE CHECK AND CHALLENGE ME ON THIS
> or throw back recommendations
>
> - Endpoint: GET https://api.fred.stlouisfed.org/fred/series/observations
>   params: series_id, api_key, file_type=json, optional observation_start.
>   Missing values come back as the string "." — handle that.
> - The API key must come from env var FRED_API_KEY, I already created one and added it
>   in .env.
>
> Design constraints:
> - Ports-and-adapters: define an IngestionPort interface
>   fetch(series_id, start=None) -> list[Observation], and a FredAdapter that implements
>   it, so the source is swappable later. (Meaning let's say if they wanna use a
>   different ingestion like something else, it should be easily replacable)
> - Observation = a typed dataclass: series_id, date, value, source, fetched_at.
> - Harden the fetch: request timeout, a couple retries, coerce "." to missing. (Think
>   like a 10 year industry experience IC, whenever you hit an API, there might be network
>   package drop, server time out Load balancer might go down anything might happen, so
>   put a retry mechanism and handle things)
> - Package name: adp_forecast. Deps: requests, pandas, python-dotenv. Set up a minimal
>   pyproject.toml, .env.example, and .gitignore too.
>
> Definition of done:
> - A small script fetches ADPMNUSNERSA and prints the last 6 observations.
> - One unit test using a MOCKED FRED response (no live network in the test).
>
> Before writing any code: ask me about anything ambiguous, then propose the file layout
> and let me approve it. NEVER WRITE CODE without talking to me.

**What the model did:** Inspected the repo and toolchain, then launched a multi-agent
research workflow — 6 parallel verifier agents plus 3 adversarial refuters — to check the
FRED and ADP claims. While that ran, it answered the SQLite-vs-CSV and FastAPI questions.

**What I did with it:** **Rejected the approach, kept part of the output.**

The SQLite reasoning was good and I kept it — specifically the argument that the choice
should be driven by the *key structure* (a three-part vintage key) rather than by row
count, and that `sqlite3` being stdlib is a tiebreaker rather than the reason. The
FastAPI advice — build the service layer to return typed objects and add the HTTP shim
last — I adopted as a standing constraint.

The 9-agent fan-out I killed. It was burning time and tokens on something I had asked to
be a quick verification. This became a standing instruction for the rest of the session
(see Turn 2).

---

## Turn 2 — Pushback on process

**My prompt:**

> Why are you taking so much time? What are you trying to do? I told you to just verify
> things and see if my blueprint matches the system design

and shortly after:

> Keep in mind, that every response needs to be brief and to the point. Always make sure
> you are using less tokens and following proper memory management patterns. You ask me
> things before you kick of multiple agents and do random things. Make sure every
> decision you take needs to be extremely precise if not ask me so that you do not burn
> tokens.
> Every code decision needs to be vetted by me. Do not hallucinate and write before
> asking me.
> Do not commit anything. I will do the commits.
> Keep this in your memory

**What the model did:** Stopped the workflow, persisted the instructions to its memory,
and switched to direct single-command verification.

**What I did with it:** **Used as-is.** This was the most useful correction of the
session. Everything after this point was one `curl`/one script at a time, which is also
what surfaced the real bugs — see Turn 3.

---

## Turn 3 — The endpoint dead end (~40 minutes lost, worth it)

**My prompt** (after the model's own `curl` calls returned nothing):

> [pasted the Access Denied HTML from my own shell]
> I do have a FRED API KEY in the env that you can use. Can you just scour the web and
> see how we can use the FRED API?
> https://fred.stlouisfed.org/docs/api/fred/

and then, with a screenshot of a Google AI result:

> Try this one too. I just googled.

**What the model did:** A chain of failed diagnoses, in order:

1. `curl` to `api.fred.stlouisfed.org` → `HTTP 000`, connection refused.
2. **Found the first real bug:** that host does not exist. The correct host is
   `api.stlouisfed.org/fred`. My prompt in Turn 1 had it wrong; I had copied it from
   memory rather than from the docs.
3. `curl` to the correct host → Akamai `403 Access Denied`. Also 403 with a browser
   `User-Agent`, also 403 with a deliberately invalid API key, and also 403 for the plain
   FRED *website*. Reasonable conclusion at the time: my network or ISP was blocking
   `stlouisfed.org`.
4. `WebFetch` on the FRED docs → also 403.
5. Loaded `fred.stlouisfed.org/series/ADPMNUSNERSA` in the in-app browser → **worked**,
   and returned the real series metadata.
6. Navigated the browser to the API endpoint with a *bogus* key (to keep my real key out
   of a URL) → got a proper FRED JSON `400 ... api_key is not registered`. That isolated
   it: the API was reachable, so the block was client fingerprinting, not an IP ban.
7. Probed Python `requests` and stdlib `urllib` in a scratch venv → **HTTP 200 on the
   first try, no headers needed.**

Conclusion: Akamai blocks `curl`'s fingerprint specifically. Python is unaffected, so the
project was never actually blocked.

**What I did with it:** **Used as-is, and this is why the log matters.** The Google AI
result I pasted was *wrong* — it showed `url = "https://stlouisfed.org"` with no `api.`
subdomain and no path, because Google had mangled the URL in its snippet. Had the model
taken either my Turn 1 prompt or my Google screenshot at face value, the adapter would
have been built against a host that does not exist.

Dead end I would skip next time: five separate `curl` variations. Once the first `curl`
failed and a browser succeeded, testing the *actual runtime* (`requests`) should have
been the immediate next step, not the seventh.

---

## Turn 4 — Data verification results and the target-definition question

**My prompt:** (continuation — the model reported its verification findings and asked two
questions: vintage-aware storage vs. lag-shifting, and what the forecast target should
be)

> Vintage aware is my pick, cause the back test needs to be perfect. And it will give us
> a solid base.
> Plus I did not get the question 2 and You did not summarize properly in regards to the
> one I gave you
> [re-pasted the six indicator URLs]
> Cause those are weekly job less claims, how many stay unemployed etc. so which data is
> getting extracted for this and ingested?
>
> You are doing way more fluff in the response. GOING FORWARD MAKE SURE YOU ANSWER THE
> QUESTION AND KEEP IT CONCISE. DO NOT ADD FLUFF to your answers.

**What the model did:** Ran the verification and produced the per-series table I had
asked for. Verified findings:

- `ADPMNUSNERSA` — **my claim was correct.** "Total Nonfarm Private Payroll Employment",
  SA, Monthly, 198 observations, 2010-01 → 2026-06.
- `NPPTTL` — **my claim was correct.** Title ends `(DISCONTINUED)`, last observation
  `2022-05-01`.
- `"."` missing values — **my claim was correct.** 16 in `NPPTTL`, 1 in `UNRATE`.
- All six indicators exist.

Things I had wrong or had not considered, which it found:

- **Units.** ADP publishes `Persons` (`132,722,000`); BLS publishes `Thousands of
  Persons` (`135,613`). A 1000× error waiting to happen.
- **`JTSJOL` is two months stale**, not one, at forecast time.
- **`PAYEMS` is total nonfarm** and includes government; `USPRIV` is the correct private
  comparator.
- **129 series in the ADP release**, including `ADPWNUSNERSA` — the same target measured
  *weekly*. I had not known this existed. Flagged with the caveat that the weekly series
  currently ends `2026-05-16` vs. the monthly's `2026-06-01`, so it may lag rather than
  lead. Went into the "next week" list rather than the build.

Then it answered the target question with data rather than argument, comparing first
print against current vintage:

```
2025-03   published +285k   now  -53k   (-338k)
2025-09   published  -91k   now  +88k   (+179k)
```

**What I did with it:** **Used as-is.** The revision numbers settled a question I had not
understood when it was posed abstractly. Seeing September 2025 published at −91k and now
reading +88k made it obvious that scoring against the current vintage measures nothing.
Decision: model the change, score against first print.

The "fluff" complaint was about the answer format, not the content. Responses got shorter
after this and stayed that way.

---

## Turn 5 — Snapshot vs. full vintage matrix, and a corrected estimate

**My prompt:**

> ahh I do see your point on the snapshot per forecast origin vs vintage matrix. you
> basically take a snapshot per prediction day instead of rummaging the entire millions of
> rows. What would be the catch all or things that could go wrong if we don't pick vintage
> and go do the snaphsot per forecast origin ? trade offs and risks.

and then, after being told what the risks were:

> 1 -- via the two-scorecard plan (~47 honest months as headline + ~184 approximate with
> a caveat).
> 4 -- you always gotta pull the release dates man, never guess you are not god lol,
> always look at the data and take that. pulling ADP's real release dates instead of
> guessing "first Wednesday."
> 5 -- using the day-before for features to avoid the same-morning leak.
> 6 -- making the collector resumable/idempotent. proper SDLC principle man.
> All of the above are good
> 3 -- You can't use revision behavior as a feature. [...] I am torn on this for sure.
> Can you elaborate what are tradeoffs on this first

**What the model did:** Listed six risks of snapshot-per-origin, ranked by severity. The
one I had not seen coming: **ADP only has 47 vintages**, so a fully vintage-correct
backtest is impossible before ~Sep 2022 regardless of storage strategy. That is a hard
data limit, and it is what the two-scorecard plan exists to handle.

Then, on the revision-behaviour question, it ran a probe before answering — and the probe
**reversed its own earlier recommendation**. It had estimated snapshot-per-origin at 1,386
requests and the full matrix at ~100k requests and "millions of rows". Measured reality:

| Approach | Requests | Rows | Payload |
|---|---|---|---|
| Snapshot per forecast origin | 1,386 | ~150k | — |
| **Full revision history** | **7** | **18,220** | **1.8 MB** |

FRED range-compresses unchanged vintages, so rows scale with the number of *edits*, not
observations × vintages. The full history is cheaper *and* a strict superset.

It also characterised revision behaviour per series: ADP has no intra-year revisions at
all (only the January rebenchmark), `USPRIV`/`PAYEMS` are revised twice with meaningful
magnitude, and `ICSA`/`CCSA` revise by a near-mechanical +1k.

**What I did with it:** **Used as-is.** The corrected estimate dissolved the tradeoff I
was torn about — with full history costing 7 requests, there is no reason not to store it
and decide about revision features later.

Worth recording as a process point: the model's *first* answer on this was wrong by four
orders of magnitude, and it was wrong because it reasoned about the data format instead of
measuring it. The fix was a probe, not more reasoning.

---

## Turn 6 — Engineering standards, then build

**My prompt:**

> Few instructions before you start writing code. BURN THIS INTO YOUR MEMORY. YOU NEED TO
> FOLLOW EVERYTHING TO A T
>
> 1. You are priniciple software engineer, your code should follow proper SDLC principles
>    and OOPS concepts.
> 2. 99% of the code can be re-used if written right, so make sure you re-use the code
>    wherever necessary.
> 3. The goal is to code to the IngestionPort interface; the contract is what stays
>    stable, adapters swap behind it.
> 4. Watch and assess what you write, Time and space complexity patterns need to be
>    checked, you need to ensure every thing is less space and time complexity.
> 5. Follow proper linting patterns, like Flake8.
> 6. Doc Strings need to be present (Public classes/methods/modules yes, skip the obvious)
>    I leave it to you
> 7. Logger needs to be there and present and logged wherever necessary.
> 8. A full blown class for our Exception class so that you can call that.
> 9. Tests, unit tests are very important for any code you write or touch, make sure tests
>    are present. Mark the live integration test optional. It should skip when there's no
>    API key/network (a pytest marker) or something similar.
>
> Do you understand and acknowledge all these when you will start writing the code?

Preceded by my approval of the proposed file layout, where the model had flagged three
deviations from my original spec:

1. `Observation` gaining `realtime_start`/`realtime_end` (my 5 fields could not express
   vintages)
2. `fetch()` gaining an `all_vintages: bool` flag
3. `pandas` being unused in the ingestion layer despite being in my dependency list

**What the model did:** Persisted the standards to memory, then wrote the layer: 8-class
exception hierarchy split on transient-vs-permanent, a source-agnostic retry module with
full-jitter backoff, the domain model, the series registry, two port protocols, the FRED
adapter, the smoke script, and 73 tests.

**What I did with it:** **Used as-is.** All three flagged deviations were correct — the
vintage fields in particular were unavoidable given the decision in Turn 4. Being told
that `pandas` is dead weight in this layer was more useful than having it quietly
imported to satisfy my dependency list.

Two bugs were caught by the live integration tests, not by the unit tests:

- **`release/dates` caps `limit` at 10,000, not 100,000** — and rejects a larger value
  with an HTTP 400 rather than clamping it. Page size is now per-endpoint, with offline
  tests pinning both values.
- **FRED returns scheduled *future* release dates** — a 2024 start currently returns
  dates through December 2026. Unfiltered, the backtest would generate forecast origins
  for months that do not exist yet.

Also found: an unknown `release_id` returns HTTP 200 with an empty list, so a typo cannot
be detected by status code. Now logged as a warning.

This is the argument for keeping the live tests in the suite even though they are
optional. Both bugs were in code that passed every mocked test, because the mocks encoded
the same wrong assumption as the implementation.

---

## Turn 7 — Documentation

**My prompt:**

> Update README.md and Prompts.md first and then let's go to storage step GO

**What the model did:** Wrote this file and the README.

**What I did with it:** Reviewed and kept. The README's evaluation section documents the
scoring plan *before* any results exist, which is deliberate — it makes the plan
falsifiable rather than something reverse-engineered from whatever numbers come out.

---

## Turn 8 — Storage layer

The model proposed the schema first. I sent back two corrections.

**My prompt:**

> Alright, storage looks good. Go ahead and build it, but sort out two things first.
>
> First one is the ingest_runs table. You've got all_vintages sitting in there as a
> column but the primary key is only series_id. Keep the key on series_id and just drop
> the all_vintages column. We only ever persist the full vintage history, current vintage
> is just a filter on that where realtime_end is the sentinel, and we already agreed those
> current vintage records are display only so they never get written anyway. That means
> there's only ever one ingest worth checkpointing, so a mode column just implies we keep
> two separate datasets when we don't. If you actually spot a real case where a current
> vintage run needs its own checkpoint, stop and tell me first, otherwise go with the
> single key.
>
> Second one is scaling. Storing the raw person counts is fine, but I don't want every
> reader dividing by 1000 on its own. Someone forgets, or someone does it twice, and
> suddenly the whole forecast is off by a factor of 1000 with nothing throwing an error.
> Put that conversion in one function that everything reads through, and write a quick test
> that pulls the latest ADP number and checks it lands around 132,722. One place it
> happens, one test guarding it.
>
> On your three questions, all good. Store raw and scale on read, per series
> resumability, and no since flag so it just does a full re ingest every time. Build it
> with the same bar as the ingestion layer and flag anything you want to deviate from the
> schema before you write it.
>
> Think about this fora. minute and let me know what you think and then confirm with me
> and then we can proceed

**What the model did:** Agreed with both, and raised one consequence of the first that I
had not thought through. Dropping the mode column means storage can no longer
structurally distinguish a genuine vintage record from a display-only one — both carry
`realtime_end = '9999-12-31'`. It proposed the invariant that does separate them: a
genuine batch contains at least one row whose `realtime_start` predates its own
`fetched_at`, because a current-vintage fetch stamps every row with the fetch date.
`upsert_observations` now rejects batches failing that.

It also pushed back on my test. Asserting the *live* latest ADP value lands near 132,722
would go red next month when the July print publishes, and badly red after the January
2027 rebenchmark. It split the test: an exact pinned assertion offline against the
fixture (`132_722_000 → 132_722`), and a plausibility band live (`100_000 < x < 200_000`)
that still catches a 1000× error but survives routine data updates.

**What I did with it:** **Used as-is.** Both pushbacks were right. The vintage guard is
the kind of thing I would only have found after it silently corrupted a backtest.

On the scaling requirement, it went further than I asked. Rather than just centralising
the function, it added an architectural test that scans the whole source tree and fails
the build if any module other than `units.py` references `scale_to_thousands`, or
hand-rolls a `/ 1000`. That converts my instruction from a convention into something
mechanically enforced, which is what I actually wanted.

**A claim it made and then disproved.** The schema shipped with an index on
`(series_id, realtime_start, realtime_end)`, commented as serving the point-in-time
query. On checking `EXPLAIN QUERY PLAN` against the real database, SQLite ignored it and
used the primary key instead. Rather than leave the comment wrong, it benchmarked both:
6,160 as-of queries, identical runtime (4487ms vs 4488ms), and the index cost 784 KB —
a third of the database — plus write amplification per ingest. Reason: the table is
`WITHOUT ROWID`, so the primary key *is* the table, ordered
`(series_id, obs_date, realtime_start)`, and a `series_id` seek already returns a
contiguous run sorted in exactly the query's `ORDER BY`. Index dropped, comment rewritten
with the condition under which to revisit. Database went from 2.6 MB to 1.7 MB.

Same pattern as Turn 5: the model's first instinct was defensible and wrong, and
measuring beat reasoning. Worth noting that it caught this one itself rather than waiting
for me to push back.

---

## Turn 9 — Feature layer

**My prompt** (after the model proposed calendar-month mean vs. the BLS reference week):

> On weekly to monthly, go with the calendar month mean, not the reference week of the
> 12th. Claims are jumpy week to week and a single week gets thrown off by holidays or a
> one off spike, so averaging the whole month gives a steadier signal. That is also why
> the four week moving average is the standard way people read claims in the first place.
> The mean uses every week in the month instead of keeping one and discarding the rest.
> The reference week of the 12th lines up with how payrolls are measured on paper, but we
> are predicting the move, not rebuilding the official number, so a steadier feature beats
> a conceptually neat but noisier one. Both are fully published before the ADP release
> date, so leakage does not favor either side.
>
> Build the aggregation as a swappable rule though, one function with the method behind
> it, default set to the calendar month mean. If claims turn out to actually matter in the
> backtest we can test the reference week version cheaply. Do not build both now, just
> leave the seam.

Then, after the model reported that rebenchmark masking looked unnecessary:

> Agreed on dropping the rebenchmark masking, and your reasoning is right. [...] It also
> matches how ADP itself reports the January number, they restate the series and then
> compute the change inside that one vintage.
>
> One change to your offer. Do not keep masking around as a flag, even defaulted to off.
> That is dead scaffolding for a problem that no longer exists. Instead enforce the real
> rule structurally: the change computation must refuse to subtract two values that come
> from different vintages, and raise if it ever tries. Same idea as the units choke point.
> One guard that makes the highest impact bug impossible to bring back quietly beats a
> flag nobody will ever turn on.

**What the model did:** Before building, it tested the masking assumption we had both
written into the README. Computing MoM change *within* a single as-of snapshot made every
January clean (+106k, +107k, +183k, +22k across 47 origins, zero implausible values). The
corruption only appeared when differencing across vintages (+4,616k, +1,926k, −2,307k).
So masking was discarding ~14 real observations to avoid a mistake that only happens if
you break a rule you should not break anyway.

It then built the guard I asked for: `changes.py` requires an explicit `as_of` argument,
and refuses any subtraction whose operands were not jointly published on that date. Plus
`shares_vintage_with()` on the domain object, which catches non-overlapping realtime
windows — the rebenchmark case — independently.

**What I did with it:** **Used as-is.** Rejecting the flag was the right call and the
model implemented the stronger version without argument. Requiring `as_of` as a mandatory
keyword is better than what I asked for: it makes the vantage point impossible to leave
implicit, so the bug cannot be reintroduced by someone who simply does not think about
vintages.

**Two things it found while validating.** First, an ordering trap I had not considered:
at the release date `R` for month `T`, the snapshot *already contains* `T` — the answer.
So the forecast origin has to be `R − 1 day` for the target as well as the features, not
just to dodge same-morning publication. Verified at origin `2026-06-30`: June invisible,
May newest.

Second, its own validation script asserted no monthly change should exceed 1,500k, and
that assertion failed at −6,845k for April 2020. Not a bug — that is COVID, a real print.
It separated the shock window and re-ran: excluding March 2020 through June 2021, the
largest move across 180 months is +947k. Worth recording because that outlier is going to
matter for the forecast layer, and it was found by a check that was wrong for the right
reason.

Validated across all 185 real ADP release dates: 46 panels buildable (the rest lack the
two observations needed to form a change), zero vintage errors raised.

---

## Turn 10 — Storage layer

**My prompt** (after the model proposed the schema):

> Alright, storage looks good. Go ahead and build it, but sort out two things first.
> First one is the ingest_runs table. You've got all_vintages sitting in there as a column
> but the primary key is only series_id. Keep the key on series_id and just drop the
> all_vintages column. [...] Second one is scaling. Storing the raw person counts is fine,
> but I don't want every reader dividing by 1000 on its own. Someone forgets, or someone
> does it twice, and suddenly the whole forecast is off by a factor of 1000 with nothing
> throwing an error. Put that conversion in one function that everything reads through,
> and write a quick test that pulls the latest ADP number and checks it lands around
> 132,722. One place it happens, one test guarding it.

**What the model did:** Agreed with both and raised a consequence I had missed — dropping
the mode column means storage cannot structurally distinguish a real vintage record from a
display-only one, since both carry `realtime_end = '9999-12-31'`. It proposed the
invariant that does separate them (a genuine batch has at least one `realtime_start`
predating its own `fetched_at`) and enforced it in `upsert_observations`.

It also pushed back on my test: asserting the *live* latest ADP value lands near 132,722
would go red next month. It split it into an exact pinned offline assertion and a
plausibility band for the live test.

For the scaling requirement it went further than I asked — an architectural test that
scans the source tree and fails the build if any module outside `units.py` references
`scale_to_thousands` or hand-rolls a `/ 1000`. That turns my instruction from a convention
into something mechanically enforced.

**What I did with it:** **Used as-is.**

**A claim it made and then disproved.** The schema shipped with an index on the realtime
columns, commented as serving the point-in-time query. `EXPLAIN QUERY PLAN` showed SQLite
ignoring it in favour of the primary key. It benchmarked both — 6,160 queries, identical
runtime (4487ms vs 4488ms), index costing 784 KB, a third of the database. Cause: the
table is `WITHOUT ROWID`, so the primary key *is* the table, already ordered the way the
query wants. Index dropped, comment rewritten. Database 2.6 MB → 1.7 MB.

---

## Turn 11 — Feature layer, and killing the masking plan

**My prompt** (abridged):

> On weekly to monthly, go with the calendar month mean, not the reference week of the
> 12th. Claims are jumpy week to week [...] Build the aggregation as a swappable rule
> though, one function with the method behind it, default set to the calendar month mean.
> [...] Do not build both now, just leave the seam.

and then:

> One change to your offer. Do not keep masking around as a flag, even defaulted to off.
> That is dead scaffolding for a problem that no longer exists. Instead enforce the real
> rule structurally: the change computation must refuse to subtract two values that come
> from different vintages, and raise if it ever tries. Same idea as the units choke point.

**What the model did:** Before building, it tested the rebenchmark-masking assumption we
had both written into the README — and killed it. Computing month-over-month change
*within* a single as-of snapshot makes every January clean (+106k, +107k, +183k, +22k
across 47 origins). The corruption only appears when differencing across vintages
(+4,616k, +1,926k, −2,307k). Masking was discarding ~14 real observations to avoid a
mistake that only happens if you break a rule you should not break.

It then built the guard: `changes.py` requires an explicit `as_of`, and refuses any
subtraction whose operands were not jointly published on it.

**What I did with it:** **Used as-is.** Making `as_of` a required keyword is better than
what I asked for — it makes the vantage point impossible to leave implicit.

**Two things it found while validating.** An ordering trap: at release date `R` for month
`T`, the snapshot already contains `T`, so the origin must be `R − 1 day` for the target
too, not just to dodge same-morning publication. And its own validation script asserted no
monthly change exceeds 1,500k — which failed at −6,845k for April 2020. Not a bug; that is
COVID.

---

## Turn 12 — Forecast layer, and the result I did not want

**My prompt** (after asking it to explain the sklearn-vs-hand-rolled tradeoff):

> never winsorise man, it is fabricating stuff. Exclude covid data set.

and later:

> Both confirmed. Window C, and hand rolled ridge.

**What the model did:** Picked the COVID boundary from the data rather than guessing —
measured every month against pre-2020 volatility and found the disruption runs
2020-03 → 2022-06, *including* twelve straight months of reopening boom above 4 sd that my
instinct would have left in the training set.

Then it built the layer and ran a walk-forward — and reported that **ridge lost**. MAE
67.5 with +18.7k bias, barely better than a random walk, worse than a 3-month mean.

Rather than tuning, it diagnosed: a ridge intercept is the training mean, 72% of history
predates 2020 averaging +180k against +54k since 2024. The model was anchored to a labour
market that no longer exists. It explicitly refused to search the training window against
backtest MAE, on the grounds that it is test-set tuning.

**What I did with it:** **Used as-is, and this was the most valuable turn.**

I told it not to treat beating the baselines as the objective — the honest number plus the
diagnosis *is* the deliverable — and to keep the refusal to tune exactly as it was. Then
approved Option A (a trailing-mean term, justified by the diagnosis rather than the score).

Result: **bias +18.7k → +3.0k**, MAE 67.5 → 62.1. Fixed by the predicted mechanism.

It also caught that its own first comparison was invalid — ridge scored on 39 origins
against baselines on 46, because the trailing mean needs history the earliest origins lack.
Comparing models over different subsets is not a comparison.

Then it recommended **against** Option B (exponential weighting), on the grounds that the
defect B was meant to fix no longer existed, so adding it would be justified only by hoping
the score improved. I agreed.

---

## Turn 13 — Evaluation layer, and a defect that turned out to be mine

**My prompt:**

> Let's get these two things done first. And then lets discuss more

(referring to its own two proposals: build the eval layer so the numbers are reproducible,
and investigate the 72% interval coverage.)

**What the model did:** Built the backtester, then used it to investigate the coverage
number — and found that **the 72% was a bug in its own scratch script**, which divided the
covered count by 46 origins when ridge had only forecast 39. Real vintage coverage is 85%
against a nominal 80%: over-covering, the conservative direction.

It then found a *real* coverage defect in the approximate scorecard and diagnosed it
properly. Interval width tracks the CV residual pool; that pool is drawn from the whole
training history, which has different error dispersion than the forecast month:

| scorecard | realised error sd | residual pool sd | ratio | coverage |
|---|---|---|---|---|
| vintage | 87.9k | 118.3k | 0.74 | 85% (over) |
| lag_shifted | 64.7k | 53.4k | 1.21 | 71% (under) |

The two miss in opposite directions, ruling out a constant correction. It also formed its
own hypothesis — that alpha selection was double-dipping on the same folds the interval is
built from — tested it, and **refuted it**: pinning alpha moved coverage under one point.

It left the defect uncorrected, on the same test-set-tuning grounds as before, and
documented it in both the code and the README.

**One real bug it found and fixed:** `build_lag_shifted` truncated weekly series at the
*first* of the month, dropping every week of the target month, so claims were unavailable
at every lag-shifted origin and that scorecard produced nothing at all. Weekly series are
dated by week-ending Saturday; the cutoff has to be month-end.

**What I did with it:** **Used as-is.** The headline finding is worth recording plainly:
the approximate scorecard reports ridge at MAE 48.6 against 62.1 on the vintage-correct
one. The approximation flatters the model by 22%, which is the clearest possible argument
for having built vintage-aware storage in the first place.

---

## Turn 14 — Explanation layer

**My prompt:**

> Let's add the prompts and add the explanation layer

**What the model did:** Built it, and treated the interesting problem as *what to refuse
to say* rather than how to phrase things. Three constraints, each with a test:

1. A consistency guard — `ExplanationError` if the named drivers do not reconstruct the
   reported number. Prose that drifts from its own arithmetic would look completely
   convincing.
2. It caught itself hardcoding backtest figures into the caveat text (*"MAE about 62,000
   jobs against 63,400 for a 3-month mean"*), which would silently become false the next
   time the backtest changed. Replaced with a `ScoreCard` parameter, and the verdict
   wording is computed from the numbers — there is a test that feeds it a *losing* model
   and asserts the caveat says so.
3. No causal language, and stale inputs / excluded regime / interval limitation all
   disclosed.

**What I did with it:** **Used as-is.** The hardcoded-caveat catch is the sort of thing
that would have shipped and quietly misled a reader months later.

---

## Turn 15 — CLI

**My prompt:**

> one unified command that ties all these together, a single entry point with subcommands
> so it becomes adp-forecast forecast, adp-forecast history, adp-forecast backtest,
> adp-forecast ingest. Let's build the cli first.

**What the model did:** Built a typer app with the four subcommands, a shared
`--db`/`--log-level` callback, and one central failure handler so every command reports a
missing database or a missing API key identically. Rendering was split into its own
module so command wiring and output formatting can change independently.

Rather than leave the four existing scripts duplicating the logic, it reduced them to
three-line shims that delegate to the CLI — one implementation to maintain, and the paths
already written into the README keep working.

It also added `--json` to `forecast`, which cost almost nothing because the service layer
had been returning dataclasses from the start, and is the concrete payoff of that
decision back in turn 1.

**What I did with it:** **Used as-is.** 30 CLI tests, including one asserting that the
text and JSON renderings describe the same forecast — two renderings of one typed object
should not be able to diverge.

---

## Honest summary of AI's contribution

**Where it earned its keep:**

- Caught that my API endpoint host did not exist, before any code was written against it.
- Caught the `Persons` vs. `Thousands of Persons` units mismatch.
- Found the vintage/revision problem and quantified it (September 2025: published −91k,
  now +88k), which redefined the forecast target and the entire evaluation design.
- Found `ADPWNUSNERSA` and the other 128 series in the ADP release, which I did not know
  existed.
- Corrected its own cost estimate by four orders of magnitude once it measured instead of
  reasoned.

- Reported that its own model lost to a 3-month mean, rather than quietly tuning until it
  did not. It named test-set tuning as the reason it would not search the training window,
  and held that line for the rest of the project.
- Caught two invalid measurements of its own: comparing models scored over different
  origin subsets, and a coverage denominator bug that had invented a defect that did not
  exist.
- Found the weekly-truncation bug that silently produced an entirely empty scorecard.

**Where it needed managing:**

- Defaulted to a 9-agent research fan-out for what was a handful of `curl` calls. Had to
  be told to stop, and told again to ask first.
- Verbose early on. Needed explicit instruction to answer the question and stop.
- Its first cost estimate on the vintage question was confidently wrong. It reasoned from
  the API's documented shape rather than measuring a response, and the error was only
  caught because I pushed back on a related question.
- Burned five `curl` variations on a fingerprinting problem that one test against the
  actual runtime would have isolated immediately.

**The pattern across all four of the useful findings:** every one came from running a
probe against the live API, not from the model's prior knowledge. The prompts that worked
were the ones that said *don't take my word for it, go check* — and the ones that failed
were the ones where either of us reasoned about the data instead of pulling it.
