# Offline P0 gates

## MotionDecode resident mode

The adapter uses the existing resident `status` contract: `accepted: true`,
`state: READY`, and `mode: dry-run` or `mode: real`. It requires the exact mode
matching the caller at connection, preflight, and immediately before every
`execute`. Missing, unknown, or mismatched mode fails closed without sending
execute. A failed status gate remains latched for that adapter instance.
The integrated supervisor's existing dedicated dry-run socket remains unchanged.

No new fields are sent to the external resident. The resident implementation is
outside this repository. A worker replacement **between** the status response
and execute cannot be fenced by the verified protocol here. Atomic validation
of caller mode and worker session at execute requires a coordinated external
worker/client protocol change; this client check is not that guarantee.

## Explicit Wander approval

Remote Wander requires a real robot selection (`g1`, `g1-ssh`, or `motiondecode`),
`--enable-real-robot`, and the new default-OFF `--operator-approved-wander` flag.
This flag explicitly confirms site readiness and authorizes real locomotion.
The selected Reaction adapter's existing gates still apply (including
`--confirm-site-ready` for real MotionDecode). Mock/dry callers are rejected.
The CLI and `RemoteWanderController` both enforce the caller approval boundary.
The controller accepts corresponding keyword arguments, all safe by default.

The legacy shell launcher requires that same approval argument before any SSH
readiness check and forwards it only when supplied by the operator. Its existing
`--dry-run` path never adds approval. Non-Wander paths need no Wander approval.
No launcher, remote worker, or hardware was used to validate these gates.
