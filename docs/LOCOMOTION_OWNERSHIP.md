# F05 Phase 1A: cooperative ownership

This protects one Patrol relay owner (Level 1) and cooperating, migrated SDK
writers (Level 2). It does not establish robot-global exclusion (Levels 3/4).

## Migrated writers

- `patrol/locomotion_relay.py` when armed.
- `patrol/locomotion_adapter.py::LocomotionAdapter` (direct SDK path).
- `robot_side/adapters/g1_robot.py::create_loco_client`, used by the current
  one-shot diagnostic.

Each acquires `/tmp/g1-project-locomotion.lock` before SDK writer initialization.
Linux `flock(LOCK_EX | LOCK_NB)` owns the open descriptor until process exit,
including initialization failure and adapter close. There is no CLI/environment
override, unlock API, stale-PID deletion or lock-file replacement. Tests inject
a temporary path. The fixed file must be accessible to the cooperating service
accounts; a permissions error fails closed. Provision one shared inode/group;
never unlink it while any writer could be alive. Different containers/hosts with
different `/tmp` mounts are not covered. Normal exec closes the descriptor;
forked processes may retain it until the last inherited descriptor closes.

OS release after death is NOT physical stop. In particular, a finite one-shot
command may still be active after the issuing process exits. That command's
physical lifetime and safe transfer remain F04 work.

## Relay protocol

Protocol v4 (F04-A) uses `operation`, `sequence`, and nested `velocity`. It never sends
legacy top-level `seq`/`vx`/`vy`/`vyaw` packets. An old relay cannot decode these
as Move. There is no fallback or retry after handshake failure.

The relay generates a UUID4 epoch at startup. `discover` returns that epoch;
`claim` must name it and a client nonce. A successful claim binds a relay-generated
UUID4 owner session to the source IP/port and starts MOVEMENT_HELD. A second claim
is rejected, including one from the same endpoint. Lost claim responses require
relay restart; they do not justify takeover.

Owned operations require the exact epoch, session, source, increasing sequence,
and movement generation. Validation-rejected packets do not update sequence/freshness.
The watchdog still runs independently of packet acceptance, including under
invalid traffic. Session identifiers prevent accidental stale senders/replays;
they are not a replacement for network authentication against hostile spoofing.

`enable` requires a committed STOP, changes generation and enables movement. `hold` changes generation and
disables movement BEFORE attempting StopMove, retaining the owner. A delayed old
Move cannot pass either while held or after a later enable. Valid Move responses
mean protocol acceptance only. HOLD responses carry prepared RPC results;
`commit_stop` binds the transaction to the still-held relay session before confirmation.
they are NOT physical stationary acknowledgements. See STOP_TRANSACTIONS.md.

The existing 0.40-second Move watchdog starts on enable and refreshes only on
accepted Move. Expiry latches FAULT before attempting STOP. Later Move, enable,
and claim are rejected. An absent owner while held stays owned until restart.
There is no release, reset or automatic takeover procedure in Phase 1A.

The watchdog and SDK calls still share a loop. RPC blocking, relay death and the
relay-check-to-SDK scheduling gap remain limitations. No hard physical stop-time
guarantee is made.

## Patrol integration and deployment

The UDP adapter discovers and claims, but never enables from `move()`. The
controller explicitly enables after its F03 lease and movement guards pass,
including normal continuation after obstacle/stage STOP. Operator resume also
enables after the existing telemetry barrier. F03 is rechecked after enable.
The owned real relay path requires a supervisor control lease; standalone mock
and dry-run paths remain independent. A protocol failure latches the client;
subsequent movement cannot automatically re-claim or re-enable.

Deploy relay and client together. Standalone relay copies must include sibling
`ownership_lock.py`, `stop_rpc.py` and `locomotion_protocol.py`; the client also needs
`locomotion_session.py` and `ownership.yaml`. Missing modules or old protocol
fail closed. Do not deploy or launch on hardware as part of offline validation.

## Not migrated

Reactive Wander and forward-distance utilities exist only on the older Wander
branches, not this base. They, old relay copies/checkouts, unknown DDS clients,
Unitree services and manual controllers remain outside this cooperative lock.
Phase 1B must migrate the actual deployed Wander writers in a separate review.
F05 is closed only for the defined active-writer Level-2 source scope when paired
with the reviewed Phase 1B Wander deployment; deployment verification is pending.
F04-A RPC confirmation is implemented; F04 physical stationary remains open.
