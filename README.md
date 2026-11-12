# V7 Paper Trading and Operations

[![CI](https://github.com/WilBGit/v7-paper-trading-system/actions/workflows/ci.yml/badge.svg)](https://github.com/WilBGit/v7-paper-trading-system/actions/workflows/ci.yml)

V7 is a research and paper-trading operations system for structured signal evaluation, release controls, health checks, and evidence-gated promotion. This repository is a sanitized source publication for review, reproducibility, and controlled maintenance.

## What is included

- Root-level `v7_*.py`, `v7_*.sh`, and release files: V7 paper signal engine, scanners, reports, quote and market-data adapters, health checks, release metadata, macro-event handling, and operational helpers.
- `validation/`: regression suites for release identity, paper boundaries, signal quality, live-adapter isolation, health recovery, and transport behavior.
- `tests/`: safety, audit, premarket, release-control, and regression checks.
- `VALIDATION.md`: the publication test snapshot and the one known audit compatibility issue.

The root-level V7 source includes reference implementations for broker-facing and live-adapter boundaries so their safety behavior can be reviewed. They are not deployment credentials, broker state, or authorization. No credential, account state, order ledger, database, log, or runtime snapshot is included.

## Safety boundary

This repository does not authorize live trading. V7 remains dry-run or paper-only unless a separately supervised release process confirms every required gate. The publication intentionally excludes local authorization state, broker tokens, account identifiers, OAuth files, private keys, trade ledgers, and runtime databases.

Do not copy a token, password, account number, broker payload, Discord credential, or local state file into this repository. Provider credentials must be supplied at runtime through a local secret manager or environment variables and must never be committed.

## Research status

The aggressive high-frequency research variant met the requested trade-count threshold but did not show a positive edge:

- MNQ: 292 trades, 52.4% wins, `-30.56R`, profit factor `0.78`.
- NQ: 299 trades, 52.5% wins, `-30.17R`, profit factor `0.79`.

Those results are preserved as research context, not as a deployment recommendation. The system must not be promoted to live trading based on these results.

## Review workflow

1. Read the release metadata and safety documentation before changing code.
2. Run the relevant regression suites in an isolated environment.
3. Run a high-confidence secret scan before every push.
4. Review source identity, configuration identity, and runtime identity separately.
5. Keep paper ledgers and runtime state outside Git.
6. Treat any failed safety gate as a stop condition, not as a reason to weaken the gate.

The source is intentionally published with operational detail while keeping secrets and machine-specific state private.
