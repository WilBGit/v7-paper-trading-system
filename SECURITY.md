# Security Policy

## Scope

This repository contains sanitized V7 research and paper-trading source. It is not a secret store, broker deployment package, or authorization record.

## Never commit

- Broker, brokerage, exchange, Discord, email, GitHub, or AI-provider credentials
- Passwords, API keys, access tokens, cookies, OAuth files, private keys, certificates, or connection strings
- Account numbers, customer data, local authorization state, trade ledgers, journals, SQLite databases, logs, or runtime snapshots
- `.env` files or copied shell environments
- Unredacted screenshots, webhook URLs, signed requests, or order payloads containing account information

## Safe configuration

Use a local secret manager or environment variables. Keep local secret files owner-readable only. Use placeholder names without values in documentation and examples. Never paste a secret into an issue, pull request, commit message, test fixture, or chat.

## Before pushing

- Inspect staged file names for state, database, log, credential, and cache artifacts.
- Run `git diff --cached --check`.
- Scan staged text and reachable history for high-confidence secret patterns.
- Compile or run the relevant regression tests in an isolated environment.
- Confirm the branch and repository visibility before pushing.

## If exposure is suspected

Stop using the value, revoke or rotate it with the provider, remove it from current files, rewrite reachable history if necessary, and re-scan the remote repository. Do not publish the value while explaining the incident.

## Trading safety

Publishing this source does not authorize live orders. Any live-adapter code is reference material and must remain behind independent authorization, release-identity, risk, and paper-safety gates.
