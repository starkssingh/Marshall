# ADR 0015 — Vault gate tokens

- **Status:** accepted
- **Date:** 2026-09-26
- **Tasks:** DS-004 (GATE-002 issues the tokens; refines ADR 0010 §4)

## Context

ADR 0010 made the catalog refuse any request past `vault.start` and kept `allow_vault=True` as a
placeholder that always raised. DS-004 asks that loaders raise `VaultAccessError` for vault data
unless given a gate token from GATE-002, and that token use be logged. GATE-002 (Sprint 13) is the
one-time vault evaluation procedure: "second vault access for same bundle refused".

## Decision

1. **One check for every loader.** `xq.datasets.vault.check_window(cfg, start, end, *, token,
   engine, run_id, purpose)` decides every read. A window ending at or before `vault.start`
   passes without a token; a window reaching past it needs a valid token. The catalog's
   `load_ticks` and `load_bars` take `vault_token=` instead of `allow_vault=`.
2. **Token shape.** A token is `<token_id>.<secret>`. The database (`vault_tokens`, migration
   0003) stores the token id, the SHA-256 of the secret, the bundle it was issued for, who issued
   it, when it expires, whether it is revoked, and the run that redeemed it. The secret is never
   stored, logged or printed.
3. **One run per token.** The first run that presents a token redeems it; the same run may read
   several windows with it (a vault evaluation loads bars and ticks), and any other run is
   refused. Revoked, expired, unknown or wrong-secret tokens are refused. Verification needs the
   metadata database and a run id, so the catalog takes an optional `engine` and `run_id`.
4. **Every access is recorded.** Each granted read logs a `vault_access_granted` warning (token,
   bundle, run, purpose, window) and adds a row to `vault_access_log`.
5. **No issuance in the library yet.** Nothing in `xq` can create a token. GATE-002 will add the
   issuing procedure (after the gate evaluation passes, one token per bundle). Until then the
   vault cannot be opened through the library; tests insert token rows directly to exercise
   verification.
6. **Datasets never read the vault.** `DatasetSpec.vault_policy` accepts only `exclude`; the
   dataset builder calls the same check with no token, so a dataset window past `vault.start` is
   refused.

## Consequences

- Opening the vault leaves three traces: the token's redemption, the access-log rows and the log
  warnings. GATE-001 can cite them as evidence.
- `xq validate --include-vault` (ADR 0013) is a pipeline stage, not a research read, and uses its
  own explicit confirmation until GATE-002 replaces that with a token.
