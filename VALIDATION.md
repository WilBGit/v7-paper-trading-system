# Validation snapshot

This publication was compiled and scanned before upload.

## Passing checks

- `tests/v73_safety_regression.py`: 206 tests passed, 22 skipped.
- `tests/v7_release_control_regression.py`: 5 tests passed.
- `tests/dream_regression_suite.py`: 5 tests passed.
- `tests/premarket_regression_suite.py`: 6 tests passed.
- `tests/signal_quality_regression.py`: 10 tests passed.
- Python compilation completed for the repository source.

## Known compatibility issue

`tests/trading_audit_regression_suite.py` currently reports two errors because the audit module calls an `exists` attribute that is not present on the current `v7_scalp_paper` module. The publication does not alter the trading engine or weaken the audit. This is recorded as a source compatibility issue for a separately supervised fix.

The failing check does not authorize live trading and does not change the paper-only boundary. Any future fix must preserve release identity, paper safety, and the existing strategy logic.
