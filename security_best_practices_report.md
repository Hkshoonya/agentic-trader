# Autonomous Trading Launch-Readiness and Security Review

Date: 2026-09-20
Scope: Python trading daemon, Robinhood MCP integration, strategy/evidence
pipeline, local dashboard, state/journal storage, dependencies, and the active
user-service deployment.

## Executive verdict

**Not verified or ready for public/live autonomous launch. Keep the strategy in
shadow mode.**

The software is substantially safer after this review and its automated suite
passes, but the trading claim does not clear its own corrected evidence gate.
There is no defensible promise of winning, and the current data specifically
argues against live deployment:

- The daemon and dashboard were restarted on the final reviewed code at 10:02
  EDT. Both services are active with zero restarts; persisted mode and promotion
  stage are `shadow`, and `arm.json` is explicitly `armed: false`.
- The newest exact-floor walk-forward fold returned **-3.741%** on the runtime-
  equivalent fixed-dollar path, and **-9.460%** with modeled costs doubled.
- At the ordinary 1% ceiling, the simulation turns **$49.90 into $7.26
  (-85.449%)**, with **87.193% maximum drawdown** and block-bootstrap p-value
  **1.0000**.
- The requested floor path starts at 10.2204% of current equity but correctly
  keeps later orders near $5.10 instead of compounding that percentage. Full
  history reaches $225.95 (+352.801%) with p=0.0085, but maximum drawdown is
  **33.112%**, only 3/6 folds are profitable, the latest three folds lose money,
  and the newest fold loses **9.460%** under doubled costs. It is therefore not
  a qualified current edge.
- Every ordinary size from 0.5% through 3% loses money under the current
  conservative $0.10 round-trip fixed-cost assumption. The exact floor is
  positive in aggregate but still does not remain inside the 15% drawdown
  ceiling; no tested size qualifies.
- Prospective evidence has **0 completed shadow trades over 3.61 days**, versus
  the new minimum of 30 completed trades over at least 30 days with positive
  realized P&L.
- The execution-cost estimate comes from one XLM round trip. It invalidates the
  zero-fixed-cost report, but is not enough to establish a stable fee/slippage
  model across instruments and order sizes.

Robinhood's own Agentic Trading documentation says an autonomous agent can place
trades without per-order confirmation and that the account owner remains
responsible for those trades. That is why stale-code activation and evidence
gates are treated here as launch blockers, not ordinary warnings:
<https://robinhood.com/us/en/support/articles/agentic-trading-overview/>.

## Launch-blocking findings

### AT-001 — Active live deployment had not loaded the final safeguards

Severity: **Critical operational finding**
Status: **Remediated operationally on 2026-09-20**

Observed state during the review:

- `agentic-trading.service` was active with a process started at 06:49:13 EDT.
- persisted mode: `live`
- persisted promotion stage: `live`
- persisted arm latch: `armed: true`, source `auto`
- current corrected arming verdict: unavailable because the report modeled
  $0.00 fixed round-trip cost while execution measured $0.10
- the service environment sets the autonomous machine capability
  `AGENTIC_ALLOW_AUTONOMY=1`; the revalidated workspace arm latch is now also
  required before submission

Impact: a long-running Python process retains the code it imported at startup.
Passing tests against the edited source tree does not retrofit those protections
into that process. Current cost-size refusals reduce immediate exposure but are
not a substitute for activating the corrected gate.

Safest remediation:

1. Explicitly disarm and put the persisted mode in `shadow`.
2. Restart the daemon and dashboard so both load the reviewed source.
3. Verify that the new startup regrade records `eligible: false`, stage
   `shadow`, mode `shadow`, and effective arm status `false`.
4. Do not regenerate a promotion report as a way to bypass the forward gate.

After the operator authorized implementation, the account was disarmed and its
mode/stage reset to shadow. Following the $5 conditional-floor implementation,
both services were restarted again on the final reviewed source at 10:02 EDT.
Startup regraded the exact-size schema-4 report as ineligible, recorded seven
retrospective and two prospective blockers, retained shadow mode/stage and the
false arm latch, confirmed the report's capital, sizing, and strategy-book
identity are current, applied 0.25%/1% effective order/day limits, and passed
all six runtime self-checks. No real order was placed or canceled during either
cutover.

### AT-002 — No presently supported winning/live strategy

Severity: **Critical financial-risk finding**
Status: **Open; strategy must remain shadow-only**

Read-only evidence rebuild over 19 instruments / 36,366 bars, using the one
measured $0.10 round-trip as a conservative fixed cost:

| Size per order | Trades | Expectancy (bps/trade) | Max drawdown | $50 final equity |
|---:|---:|---:|---:|---:|
| 0.50% | 396 | -12,714.932 | 76.343% | $12.31 |
| 0.75% | 465 | -11,062.996 | 86.530% | $7.28 |
| 1.00% (ordinary ceiling) | 486 | -8,279.975 | 87.193% | $7.26 |
| 1.50% | 522 | -5,288.644 | 86.980% | $8.10 |
| 2.00% | 525 | -3,130.726 | 81.517% | $12.50 |
| 3.00% | 525 | -1,221.143 | 71.498% | $22.70 |
| Runtime-equivalent ~$5.10 fixed-dollar floor | 506 | +682.197 | 33.112% | $225.95 |

Every ordinary size from 0.5%-3% loses money under the current conservative
fixed-cost model. The runtime-equivalent floor path is aggregate-positive and
its p-value clears 0.05, but its 33.112% drawdown exceeds the 15% gate, only
3/6 folds are profitable, both recent folds lose money, and the newest fold
loses 3.741%. Under doubled costs, aggregate return is +246.901%, but drawdown
rises to 43.560% and the newest fold loses 9.460%. These figures invalidate
current autonomous readiness—not forecast future performance from a one-round-
trip cost sample. The unusually strong aggregate result is concentrated in old
regimes; the latest three folds are all negative.

The corrected gate is implemented in
[`promotion.py`](src/agentic_trading/promotion.py): it requires current costs,
positive aggregate account return, two consecutive profitable recent folds with
at least 10 trades each, survival when all modeled execution costs are doubled,
a prospective sample/duration, and positive prospective P&L. Statistical
significance is now block-bootstrapped from daily marked-to-market account
returns instead of treating correlated trades as independent. The prospective
record is constructed from actual shadow journals in
[`evidence.py`](src/agentic_trading/evidence.py).

Safest remediation: gather prospective, cost-attributed shadow evidence; do not
optimize new parameters repeatedly against the same history; and reject the
strategy if the pre-registered forward thresholds are not met.

### AT-003 — Configured size override materially exceeded supported risk

Severity: **High**
Status: **Remediated in the active configuration**

At discovery, the approximately $49.90 account's schedule permitted 22.2635%
per order and roughly 89% daily opening notional, while
`accept_evidence_override = true` waived the evidence-size mismatch. The fresh
cost-aware run supports no tested size inside the 15% drawdown ceiling.

The active configuration now sets `accept_evidence_override = false`, uses no
account-size schedule, caps ordinary orders at 1% and the day at 4%, and
hard-caps any future schedule at 1%. A separate 11% order/day ceiling can fit
one approximately $5.10 opening on the current $49.90 account, but it activates
only after a schema-4 report tests that exact fixed-dollar/daily-cap path and passes all
retrospective return, recent-fold, significance, doubled-cost and drawdown
checks. The current report fails seven retrospective checks, so runtime keeps
the ordinary caps and makes no floor-sized entry. Even a future retrospective
pass would authorize shadow forward collection only; live trading still needs
30 completed trades over 30 days with positive P&L, promotion, machine
capability, and a valid arm latch.

### AT-004 — Hard kill switch also blocked verified exits

Severity: **High reliability/risk finding**
Status: **Remediated and regression-tested**

[`risk.py`](src/agentic_trading/risk.py) now treats the loss/error kill switch as
an entry circuit breaker: it blocks new exposure while permitting a verified,
non-overselling sell. Broker holdings/open-order read failures still fail closed,
so an unverified or potentially duplicate exit is not submitted. Removing the
machine capability or disarming remains the hard all-submission halt.

### AT-005 — Environment arming was an evidence bypass

Severity: **Medium**
Status: **Remediated and regression-tested**

The runtime now requires both a machine capability (`AGENTIC_ALLOW_LIVE=1` or
`AGENTIC_ALLOW_AUTONOMY=1`) and a valid workspace arm latch. The latch is
revalidated against the current evidence schema and the exact report timestamp
that the promotion assessment graded. An environment variable alone cannot
submit an order. Manual and automatic arming now require the same complete
preflight; an existing latch becomes ineffective when any check fails. The
separate console accepts a health report only when a fresh daemon heartbeat
names the same still-running process, preventing both stale reports and
cross-process CLI checks from making the deployment look armable.

## Corrected findings

### Market data and configuration fail closed

- Quotes now require both a fresh local observation time and a real source
  timestamp; the normalizer no longer invents source time for undated prices.
- Paired bid/ask timestamps use the older side, including the broker's venue and
  crypto field names.
- Boolean strings such as `"false"` can no longer become truthy through Python's
  `bool()` conversion.
- Percentage/range, finite-number, schedule ordering, symbol and HTTPS endpoint
  validation now occurs during config loading.

Relevant code: [`marketdata.py`](src/agentic_trading/marketdata.py),
[`runtime.py`](src/agentic_trading/runtime.py), and
[`config.py`](src/agentic_trading/config.py).

### Order placement and accounting fail closed

- A failed open-order read blocks all live writes until a successful refresh.
- A placement exception is treated as ambiguous, because the broker may have
  accepted the write and lost only the response. Later same-cycle intents are
  blocked and the next cycle forces reconciliation.
- Internally handled broker failures now contribute correctly to the
  consecutive-error kill switch across cycles.
- Deferred retries reuse their original daily-notional reservation instead of
  double charging risk or spawning nested retry queues.
- Daily order/notional reconstruction counts unique BUY reservations, not sells,
  placement events, and retry records as separate opening exposure.
- Live restart reconciliation now trusts broker holdings; shadow restart replay
  uses all date-named journals and only shadow fill records.

Relevant code: [`runtime.py`](src/agentic_trading/runtime.py),
[`risk.py`](src/agentic_trading/risk.py), and
[`journal.py`](src/agentic_trading/journal.py).

### Evidence cannot silently promote stale performance

- Fixed per-order execution fees are charged on entry and exit in walk-forward
  simulation.
- Schema 4 reproduces the small-account runtime path: the ~$5.10 target stays
  fixed in dollars as equity changes, the daily opening-notional cap is applied,
  and the floor stops when the target no longer fits both 11% ceilings. The
  superseded model compounded the initial 10.22% share and admitted multiple
  same-day entries, so none of its headline results are retained.
- A CLI percentage override is explicitly a hypothetical experiment and cannot
  authorize the fixed-dollar floor; promotion requires matching floor-model
  metadata as well as a sufficient initial percentage.
- A positive measured round-trip cost immediately becomes a conservative fixed
  fee instead of leaving the model artificially free until five fills exist.
- Stored reports are compared against the current cost model before arming.
- Stored reports are also compared against the exact potential production size;
  changing the $5 target, account capital, confidence cap, floor ceiling, or
  daily opening budget forces a schema-4 rebuild rather than reusing evidence
  for a different sizing path.
- Stored reports are bound to the current strategy, base sizing mode, position
  count, and complete effective symbol universe. Changing any of those inputs
  invalidates and rebuilds the evidence instead of allowing results from a
  different book to authorize the runtime.
- The most recent walk-forward fold must have positive account return.
- The trend strategy needs at least 30 completed prospective shadow trades over
  at least 30 days and positive realized P&L.
- A failed evidence gate immediately demotes an autonomous higher stage and
  applies the lower-risk mode; raising risk still requires autonomy consent.
- Auto-arm and manual file arming now use the same current-cost eligibility
  gate, preventing an apparently armed but unusable latch.

Relevant code: [`execution.py`](src/agentic_trading/execution.py),
[`walkforward.py`](src/agentic_trading/walkforward.py),
[`evidence.py`](src/agentic_trading/evidence.py),
[`promotion.py`](src/agentic_trading/promotion.py), and
[`arming.py`](src/agentic_trading/arming.py).

### Daily decision state and scarce-budget ordering are deterministic

- Equity and crypto now persist separate once-per-day decision guards. A single
  shared string previously toggled whenever the composite feed alternated books,
  allowing each book to erase the other's guard and repeatedly emit entries.
- Entry intents follow the same volatility-adjusted conviction rank used by the
  evidence engine. When the small-account budget admits one useful order, an
  arbitrary alphabetical ticker no longer receives it ahead of a stronger
  signal.
- Production and research now use the same EWMA volatility calculation,
  deterministic tie-breaking, rounded weights, and marked-to-market equity for
  sizing.

Relevant code: [`trend_crypto.py`](src/agentic_trading/strategies/trend_crypto.py)
and [`walkforward.py`](src/agentic_trading/walkforward.py).

### Local dashboard attack surface is constrained

- The server refuses non-loopback binds.
- Host-header validation prevents a DNS-rebinding page from using the browser as
  a localhost proxy.
- State changes require a loopback peer, exact JSON content type, POST, an
  explicit confirmation phrase for arming, and a same-local-origin browser
  Origin when present.
- Dynamic values are HTML-escaped, and responses carry CSP, frame denial,
  content-type sniffing protection, and a strict referrer policy.
- The JavaScript template's escaped regular expression is now delivered without
  Python control-byte corruption.

Relevant code: [`dashboard.py`](src/agentic_trading/dashboard.py) and
[`dashboard_js.py`](src/agentic_trading/dashboard_js.py).

The dashboard has no remote-user authentication because it is intentionally a
single-user loopback service. It is **not suitable for direct public binding or
reverse-proxy exposure** without adding real authentication, authorization,
TLS, request limits, and CSRF/session controls.

### Secrets, identifiers, filesystem, and supply chain

- A credential-pattern scan found no committed hardcoded token/private-key
  pattern in the project scope.
- OAuth token and `.env` files are owner-only.
- Runtime state and journals are now owner-only (`0700` directories, `0600`
  files); atomic state writes prevent torn reads and preserve `0600`.
- Durable journal records recursively redact account numbers, authorization and
  token fields before serialization.
- The public crypto-history fetcher accepts only HTTPS on `api.kraken.com`.
- A hash-bearing [`uv.lock`](uv.lock) was generated for reproducible installs.
- The runtime environment is recreated from that lock with `uv`, without
  retaining the old vulnerable `pip 24.0` installer.
- The final `pip-audit` path scan found no known vulnerability in the installed
  third-party packages. The local unpublished `agentic-trading` package itself
  cannot be matched against PyPI, so its security is covered by this source
  review and tests rather than a registry advisory lookup.

Relevant code: [`journal.py`](src/agentic_trading/journal.py),
[`jsonio.py`](src/agentic_trading/jsonio.py), and
[`crypto_history.py`](src/agentic_trading/crypto_history.py).

## Security checklist assessment

| Area | Result | Notes |
|---|---|---|
| Authentication | Partial | OAuth PKCE/state handling and owner-only token storage exist; a fresh full live OAuth round trip was not performed in this review. Robinhood controls primary login/MFA. |
| Authorization | Pass with broker gap | Orders are restricted to the resolved Agentic account, configured symbols, risk checks and broker review. Submission requires both a machine capability and the revalidated evidence-gated workspace latch; live broker authorization was not exercised end to end. |
| Session/token handling | Pass with gap | Tokens are not client-side and are stored `0600`; expiry/refresh paths are covered by tests, but live expiry recovery was not exercised. |
| Input validation | Pass | Strict config, price, timestamp, symbol, schedule, URL and order-shape validation; unknown broker payloads fail closed. |
| Output encoding/XSS | Pass for loopback design | Dashboard values escape HTML and security headers are set. |
| CSRF/DNS rebinding | Pass for loopback design | JSON-only POST, local Origin validation, loopback peer and Host checks. |
| SSRF | Pass in reviewed paths | MCP requires HTTPS except explicit loopback development; Kraken fetch is host/scheme constrained. |
| SQL/command/path injection | No applicable SQL; no high finding | No database or shell built from remote input. Subprocess uses fixed binaries and argument arrays. State paths remain operator-controlled local config. |
| CORS/public exposure | Pass only while loopback | No public bind is allowed. Do not reverse-proxy this console as-is. |
| Secrets/PII | Improved | No scanned hardcoded secret; owner-only files; account/token fields redacted from new journal records. Existing historical records may still contain identifiers and should be rotated or sanitized separately. |
| Dependencies | Pass at review time | `pip-audit`: no known vulnerabilities; `uv pip check`: clean; `uv.lock`: valid. |
| Abuse/rate limits | Partial | Broker polling, auto-arm and model calls have internal throttles. There is no public HTTP service; if that changes, add authentication and per-client rate limits. |
| Production configuration | Blocked for live | Runtime state and limits are safely shadow/disarmed and both services are active, but the strategy fails current-cost evidence and the cost model still has only one measured round trip. |

## Verification performed

- Full test suite: **820 passed, 92 subtests passed** on Python 3.12.3 and again
  on Python 3.14.4.
- Focused sizing parity, evidence, promotion, strategy-state, runtime and
  dashboard regression run: 232 passed.
- Dashboard/arming tests, including Host, Origin, JSON content type, loopback and
  cost-gate checks: passed.
- Python compilation with warnings promoted to errors: passed.
- Ruff correctness rules (`E4,E7,E9,F,B`) over `src` and `windows`: passed.
- Bandit medium/high scan over `src` and `windows`: passed with no findings.
- `uv pip check`: no broken requirements.
- `pip-audit`: no known vulnerabilities in installed third-party packages.
- `uv lock --check`: passed.
- Clean isolated locked Python 3.14.4 environment followed by the full suite:
  820 passed, 92 subtests; the operational `.venv` remained on locked Python
  3.12.3.
- Secret-pattern scan: no match in the reviewed source/config/documentation
  scope (runtime state and journal contents were excluded from disclosure).
- Read-only broker/runtime observations: account/equity parsing, quote shapes,
  persisted mode/stage/gate state and service state were inspected. No order was
  placed, canceled, or modified by this review.

## Remaining verification gaps

The project must still be treated as not verified for public/live launch until
all relevant gaps are closed:

1. No real order was placed by this review, so partial fills, delayed order-book
   visibility, cancel/replace behavior, idempotency after network loss, and
   broker-side rejection details remain unverified end to end.
2. The single XLM round trip is not enough to model cost by symbol, side,
   volatility, spread, notional, and time of day.
3. The prospective strategy sample is empty: 0 completed trades.
4. A fresh full OAuth login/refresh/expiry/logout path was not exercised.
5. Robinhood's current documentation says Agentic crypto availability is
   state-dependent, including unavailability in New York. System timezone is
   not proof of residence; the operator must verify account eligibility:
   <https://robinhood.com/us/en/support/articles/agentic-trading-overview/>.
6. Alert delivery, machine sleep/reboot recovery, log rotation, backup restore,
   clock skew, disk-full behavior, and network partition recovery need a
   controlled operational drill.
7. The verified-exit behavior is unit-tested, but an end-to-end drill must still
   confirm exits during daily-loss and ambiguous broker-error scenarios.
8. Historical journal files created before recursive redaction may contain
   broker account identifiers. They were permission-hardened, not rewritten, to
   preserve audit integrity.

## Required path to a controlled canary

1. **Completed:** disarm, reset mode/stage to shadow, and activate the reviewed
   runtime/dashboard code.
2. **Completed:** set `accept_evidence_override = false`, remove the aggressive
   account-size schedule, enforce 1%/4% ordinary limits, and make the requested
   $5 floor conditional on exact-size retrospective evidence with one opening
   per day.
3. **Completed:** rebuild schema-4 evidence under the current capital, cost,
   fixed-dollar target and daily-cap assumptions; it fails seven retrospective
   checks and the floor remains held.
4. Collect multiple attributed round trips at representative symbols and sizes
   without using production capital beyond an explicitly approved test budget.
5. Freeze the strategy/hyperparameters, then accumulate at least 30 completed
   shadow trades over at least 30 days; require positive realized P&L after the
   measured cost model.
6. Run an end-to-end fault-injection drill for quote staleness, holdings/open
   order read failures, ambiguous placement response, partial fills, token
   expiry, restart, disk full and network partition.
7. Drill the entry-breaker/verified-exit path and the hard halt (disarm plus
   removal of machine submission capability).
8. Only then consider a supervised canary with the smallest broker-valid,
   evidence-compliant allocation and an operator watching broker-confirmed
   orders and alerts.

Passing these steps can reduce risk; it still cannot establish a guaranteed
winning strategy or make the system unhackable.
