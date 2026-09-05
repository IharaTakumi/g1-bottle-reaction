# Navigation architecture

## Scope

This phase adds the Windows-side **Navigation capability boundary** used by the
game. It does not implement SLAM, route authoring, waypoint registration, an
Ubuntu bridge, or a Unitree-specific protocol. No ROS 2, Nav2, or SLAM Toolbox
dependency is introduced.

Navigation remains independent from `RobotAdapter`. This permits staged tests
such as `MockRobotAdapter + RemoteNavigationAdapter` and keeps Unitree SDK
imports isolated in `adapters/g1_robot.py`.

```text
Camera / Audio / Stealth game
             |
             v
   BottleReactionApp._dispatch_reaction()
             |
             v
        ReactionEngine  -- lifecycle observer --> NavigationCoordinator
             |                                      |
             v                                      v
        RobotAdapter                         NavigationAdapter
                                                /          \
                              MockNavigationAdapter   RemoteNavigationAdapter
                                                               |
                                                     NavigationTransport
                                                               |
                                                        Fake (now) / bridge (future)
```

## Common model and adapter contract

`navigation/models.py` defines only the minimum verified application model:

- `Pose2D`: `x`, `y`, `yaw_rad`, `frame_id`, and source timestamp.
- `NavigationState`: `DISCONNECTED`, `IDLE`, `PATROLLING`, `PAUSED`,
  `STOPPED`, and `ERROR`.
- `NavigationStatus`: connection, active route, pause reason, and last error.

`NavigationAdapter` divides operations by safety consequence:

| Category | Operations | Real-navigation opt-in |
| --- | --- | --- |
| Read-only | `health`, `capabilities`, `status`, `pose` | Not required |
| Fail-safe | `pause`, `stop` | Not required |
| May cause motion | `start_patrol`, `resume` | Required by the remote adapter |

The abstract adapter knows nothing about Unitree, DDS, HTTP, gRPC, or ROS.

## Mock and Remote adapters

`MockNavigationAdapter` is an in-memory deterministic state machine. It records
all commands, route IDs, pause reasons, and timestamps, supports pose updates,
and can inject connection loss or an error. It is the Windows integration-test
backend and does not need a model, camera, network, or Unitree SDK.

`RemoteNavigationAdapter` accepts an injected, protocol-neutral
`NavigationTransport`. There is intentionally no production transport yet.
Each mutating request carries a generated command ID. The adapter does not
retry mutations: when a future transport adds retry behavior, the bridge must
deduplicate repeated requests with the same command ID and return the original
result.

The CLI currently accepts:

```text
--navigation {disabled,mock,remote}
--enable-real-navigation
--navigation-endpoint ENDPOINT
--navigation-test {health,status,pose}
--start-patrol [ROUTE_ID]
```

`--navigation remote` fails with a clear message until a verified transport is
injected. `--enable-real-navigation` is checked again inside
`RemoteNavigationAdapter`, independently of `--robot` and
`--enable-real-robot`. It gates only `start_patrol` and `resume`; read-only and
fail-safe commands remain available.

## Reaction pause/resume lifecycle

Accepted reactions expose a `ReactionJob` lifecycle without importing any
Navigation type into `ReactionEngine`:

```text
accepted
   |
   +-- cooldown rejection: no job and no navigation command
   v
NavigationCoordinator.before_start
   |
   +-- if PATROLLING: send pause with reaction:<job-id>
   |                  poll until matching PAUSED is confirmed
   |                  failure/timeout => reaction FAILED, no motion
   v
started -> reaction outputs -> RobotAdapter completion confirmation
   |                              |
   |                              +-- false/timeout => FAILED, remain PAUSED
   v
completed
   |
   +-- resume only if this coordinator still owns the matching pause,
       all queued accepted reactions completed, and status is connected PAUSED
```

Operator pause, STOPPED, ERROR, DISCONNECTED, a changed pause reason, a reaction
failure, or an unconfirmed motion completion clears/inhibits automatic resume.
Recovery always requires an explicit operator action.

The completion contract is deliberately conservative:

- Mock confirms a recorded motion immediately.
- MuJoCo confirms that its animation worker reached idle.
- G1 `custom_notice` waits for the existing custom-motion controller.
- G1 preset `notice` has a conservative boundary after its configured delay and
  successful release action.
- G1 preset `spot_target`, unsupported/no-op motions, and uncertain RPC timeout
  results cannot currently prove completion and therefore leave patrol paused.

`ReactionEngine` catches per-job robot, speech, coordinator, and completion
errors. The job becomes `FAILED`, `last_error` is retained, the worker continues
to later jobs, and normal shutdown remains possible. When Navigation is
disabled, no lifecycle observer or completion wait is installed, preserving the
existing reaction behavior.

## Heartbeat, disconnect latch, and lease responsibility

The Windows client periodically invokes the transport `heartbeat` operation.
A timeout, rejected heartbeat, malformed response, disconnected status, or
`ERROR` status latches movement inhibition in `RemoteNavigationAdapter`.
Network recovery alone never clears the latch. The sequence is:

```text
fault -> DISCONNECTED/ERROR -> explicit reconnect in a connected non-moving state
      -> explicit start/resume (still requires --enable-real-navigation)
```

The client heartbeat is observability and lease renewal, not the final safety
mechanism. The future Ubuntu bridge **must own a server-side lease watchdog**:

- lease expiry must issue a local verified pause or stop even if Windows is gone;
- it must never depend on a final Windows packet;
- reconnect must report a non-moving state before the client latch can clear;
- reconnect must not resume a route;
- repeated mutation command IDs must be idempotent;
- an RPC timeout must not be automatically retried when execution is uncertain.

## Future bridge contract

`NavigationTransport.request()` currently carries an operation name, a mapping
payload, endpoint, timeout, and optional command ID. A verified bridge transport
must support these semantic operations:

- `health`, `capabilities`, `status`, `pose`
- `start_patrol(route_id)`, `pause(reason)`, `resume`, `stop`
- `heartbeat` / lease renewal

Status replies must supply `state`, `connected`, `active_route_id`,
`pause_reason`, and `last_error`. Pose replies must supply the `Pose2D` fields or
explicitly report that pose is unavailable. Protocol framing, authentication,
TLS, version negotiation, Unitree service names, DDS topics, command numbers,
route storage, and coordinate conventions must be selected only after inspecting
the real G1 SLAM service.

## Event log

`NavigationCoordinator` writes navigation events through the existing
`JsonlEventLogger`: patrol start, operator pause/resume/stop, reaction accepted,
pause requested/confirmed, reaction start/completion/failure, and command
errors. Records include state, connection, route ID, pause reason, and error.
Logging failure is isolated from motion safety.

## Windows verification

No hardware or network is needed:

```powershell
python -m g1_bottle_reaction --navigation mock --navigation-test status
python -m g1_bottle_reaction --simulate-stealth --robot mock --speech mute --navigation mock --start-patrol outer-loop
python -m pytest
```

The second command demonstrates `PATROLLING -> PAUSED -> reaction -> PATROLLING`
and writes navigation records to the configured JSONL event log.

## Next G1 session

Priority order for the next real-device session:

1. Inventory the official SLAM service actually installed on the G1: process,
   API/schema, coordinate frame, timestamps, route semantics, and safe stop.
2. Prove a local Ubuntu-side pause/stop and lease-expiry watchdog before exposing
   any start/resume operation remotely.
3. Choose and version one authenticated bridge protocol, then implement the
   Ubuntu bridge and one Windows `NavigationTransport` implementation.
4. Contract-test health/status/pose and safe commands first with
   `MockRobotAdapter`; verify disconnect and reconnect remain non-moving.
5. Only then opt in to start patrol, verify PAUSED acknowledgement and command-ID
   deduplication, and exercise reaction pause/resume at low risk.
6. Add a verified G1 preset-action completion signal before allowing those
   reactions to auto-resume patrol.

Intentionally still absent: mapping, relocation, waypoint registration, route
editing, Node/Edge DDS, LiDAR processing, obstacle avoidance, geofencing,
AprilTag support, velocity-command patrol, and a waypoint GUI.
