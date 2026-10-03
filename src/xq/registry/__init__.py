"""The model registry and the release gates (Phase 19, Phase 25).

- `xq.registry.models`: forecasting models and their versions, with statuses (MREG-001);
- `xq.registry.gates`: gate records and the status transitions they allow (MREG-002);
- `xq.registry.bundles`: content-hashed strategy bundles, their performance history and the
  active bundle of each environment (MREG-003 ... MREG-005);
- `xq.registry.evaluate`: `xq gate evaluate`, the evidence of a bundle against the gates
  (GATE-001);
- `xq.registry.vault`: the one-time vault evaluation of a validated bundle (GATE-002).

Nothing reaches paper or live trading on someone's say-so: a status moves only through a
recorded, passing gate result, enforced here and by the database's triggers (ADR 0060).
"""
