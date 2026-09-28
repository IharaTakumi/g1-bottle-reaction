# F04-A: correlated STOP RPC transactions

This confirms an RPC result, never physical stationary state. F04 physical
stationary remains OPEN. No odometry/gyro threshold, hold time, stopping distance
or stationary observer is introduced.

## Verified SDK boundary

Read-only source: unitree_sdk2_python commit
65691c8a8bc53b98d3976dba4dbf9d5d20b2e7f5.

- g1/loco/g1_loco_client.py: StopMove calls SetVelocity(0., 0., 0.) and drops its
  return. SetVelocity's default duration is 1.0; it returns the _Call raw code.
- rpc/client_base.py: _CallBase returns response.header.status.code after checking
  the API identity. Send/timeout/mismatch paths return non-success error codes.
- rpc/internal.py: RPC_OK = 0.

ResultPreservingClient calls the identical public SetVelocity operation once.
The lazy real-runtime boundary supplies the installed SDK RPC_OK and rejects an
unsupported success convention. The wire contract recognizes strict integer 0
only. None, bool, float zero, string zero, unknown codes and exceptions fail closed.
SDK files are not edited. Dry-run adapters explicitly simulate success without RPC.

## Protocol and states

Client and relay now require protocol_version 4. Actual v2/v3 dispatch rejects v4;
v4 rejects v2/v3. Legacy top-level velocity envelopes still cannot bypass ownership.
No downgrade fallback exists.

The existing request_id UUID identifies HOLD. The valid owner must also match
epoch, source endpoint, session, sequence and current movement generation.
On acceptance, the relay first switches to MOVEMENT_HELD and makes request_id the
new movement generation, then sets STOP_RELAY_RECEIVED and invokes the RPC.
Binding the new generation to the transaction UUID lets the client verify it
exactly without allocating another UUID or accepting an arbitrary response value.
STOP_RELAY_RECEIVED is relay-local progress; a lost response does not prove receipt
to the client. There is no intermediate acknowledgement or status polling.

Strict RPC success sets STOP_RPC_PREPARED, which does not authorize Reaction or
enable. The relay keeps one current STOP record with the original request UUID,
epoch, owner session, held generation, raw RPC result and committed=false.
The client sends commit_stop with a new request UUID and sequence, binding the
original stop_request_id and the same epoch/session/post-HOLD generation.
The relay validates the endpoint, ownership, prepared record and held/non-fault
state, then marks that record committed and returns STOP_RPC_CONFIRMED. Commit
does not call the SDK, change generation or enable movement. The client validates
both response identities and original STOP identity before confirming.

Failure sets STOP_UNCONFIRMED and FAULT, without a second STOP call. The response
contains request_id, operation, protocol_version, relay_epoch, owner_session,
sequence, movement_generation, state/relay_state, stop_rpc_status, raw_rpc_code
and stop_request_id. Commit validation rejection does not create a relay fault
or change authorization; it still latches the requesting client unconfirmed.
JSON scalar raw codes are retained; unknown objects/nonfinite floats have no wire
code and are reported unconfirmed. Acceptance is not stationary confirmation.

Identical duplicate HOLD datagrams are rejected by sequence/generation validation;
no second RPC is invoked. No result cache is necessary because the client never
retries a failed HOLD. Lost requests/responses and malformed/mismatched responses
all latch the client STOP_UNCONFIRMED. Later packets cannot clear that latch.
Commit duplicates are rejected by sequence/prepared-state validation without
an extra RPC. Commit timeout, rejection or ACK loss also latch STOP_UNCONFIRMED;
even a relay-side completed commit cannot authorize the client without its ACK.
Move, enable, automatic re-claim and additional HOLD attempts are prohibited after
an unconfirmed HOLD; restart/re-establishment is required. A preceding Move failure
may still attempt one HOLD, but can never re-enable the failed session.

The unchanged 0.40-second watchdog latches FAULT before a STOP attempt. Relay SDK
calls and watchdog share a loop: RPC blocking can delay watchdog evaluation.
Client deadline remains the existing 0.10 seconds per exchange (HOLD and commit);
real RPC latency and false
timeouts require later on-robot evaluation. A late successful STOP keeps the relay
held but cannot turn the timed-out client into a confirmed client.

If A exits after preparing and B starts at the same endpoint, B has a new epoch,
no owner and no A STOP record. A's delayed genuine RPC response can establish
only PREPARED; commit to B is rejected. No discover/reclaim/recovery is attempted.
Recovery requires a new control session and compatible relay initialization.
This barrier proves continuity at relay commit processing, not perpetual relay
liveness: restart after commit processing (including before ACK delivery) is a
later control-plane event and is not eliminated by this protocol. A new relay
starts held without an owner; it cannot automatically move. Physical stationary
and current-epoch telemetry remain F04-B work.

## Patrol and Reaction

Initial claimed sessions require a confirmed HOLD before the first enable.
Patrol pause acknowledgement waits for the correlated RPC confirmation. Failed
STOP latches the controller without recursively retrying STOP. Existing F03/F08
faults remain latched independently of RPC outcome. Resume requires the latest
STOP confirmation, valid F03 lease, owner session and existing telemetry gates.
New enable generations continue to reject delayed pre-HOLD Move packets.

The local interlock rejects missing/unconfirmed STOP status from IPC, including
older Patrol servers. A preparation thread waits for STOP RPC confirmation and
existing motion preflight before calling ReactionEngine.handle: neither audio nor
MotionDecode job starts on an unconfirmed STOP. Existing readiness is checked again
at motion execution. Preparation does not block the Vision loop. RPC confirmation
and motion preflight timestamps are named separately from physical stationary.

## Deployment pair and remaining work

Deploy this commit's Patrol client (adapter/session/controller), local Reaction
interlock and relay together. Standalone relay deployments must include
locomotion_protocol.py, ownership_lock.py and stop_rpc.py. Old relay/client pairs
are rejected; do not retry a launch with protocol v2/v3.

F05 canonical lock and its same-host/same-namespace active-writer scope are unchanged.
Forward-distance remains excluded. Deployment/process/inode verification is still
pending. F04-B must establish physical stationary independently of these RPC codes.
The existing P3 test gap for actual Patrol writer-entry lock contention remains a
separate test-only follow-up, outside this transaction change.
