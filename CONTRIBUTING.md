# Contributing

This is a live trading system. A change that looks harmless in a diff can move
real money, so the bar for changes is higher than for a typical project.

## The short version

1. Fork or branch — `master` is protected, so direct pushes are refused.
2. Open a pull request. The Windows build workflow runs the test suite on it.
3. A review from the code owner is required before it merges. Add a clear
   description of what could go wrong if the change is wrong.

## What a good change looks like here

- **Tests first, and tests that could fail.** Every fixed bug in this repository
  has a test that reproduces it — the crash-loop bug that stopped the agent for
  six hours has one that asserts the *original* error survives a failed startup.
- **Honesty over optimism.** Numbers reported to the operator must be
  reproducible from the raw data: expectancy from the trade list, drawdown from
  the marked-to-market curve, confidence from its named components. A change that
  makes a result look better without making it more true will be rejected.
- **Gates may only reduce risk.** The LLM advisor, the regime gate, and every
  other model-driven component may refuse a trade or make it smaller. None of
  them may create a trade, size one up, or extend a hold. This is a design
  invariant, not a preference.
- **No new switch without a default of off.** Anything that increases what the
  agent may spend must be off unless the operator sets it, and must be
  per-session environment rather than a file that travels with the repo.

## Before opening a PR

```bash
.venv/bin/python -m pytest tests -q              # 747 tests, all must pass
.venv/bin/agentic-trading selfcheck --offline --config config/agentic.example.toml
```

If you touch the Windows packaging, also run the `windows-build` workflow (or
`windows/build.ps1` on Windows) — it smoke-tests the frozen executables, which
is how two Windows-only bugs were caught.

## Reporting instead of fixing

For anything exploitable, use [SECURITY.md](SECURITY.md) rather than a public
issue. For "the strategy is wrong about X", an issue with the numbers is more
useful than a patch.

## Licence

This repository is proprietary (see [LICENSE](LICENSE)). By opening a pull
request you agree that your contribution may be used and relicensed by the
copyright holder, and you confirm you have the right to submit it.
