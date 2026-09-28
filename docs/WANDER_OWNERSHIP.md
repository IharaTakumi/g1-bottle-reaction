# Reactive Wander ownership v1

Launch path: approved integrated launcher -> RemoteWanderController ->
scripts/g1-wander-reactive-mvp.py -> UnitreeSdkRuntime.create_loco_client ->
canonical lock -> SDK/DDS initialization -> finite SetVelocity pulses.

Real CLI execution additionally requires --require-locomotion-ownership-v1.
All existing real selection, enable, execution, operator and FSM gates remain.
Legacy scripts use argparse.parse_args and reject the new flag before SDK init.
The script also passes a required ownership keyword to the SDK factory, so a
mixed deployment with a new script and old runtime fails before SDK init.
No launch without the flag is retried. Existing unmarked Wander processes must
not be accepted as ownership-compatible by the launcher.

robot_side/ownership_lock.py is copied byte-for-byte from
cf1e4d94e75fc9bb64d9941fba863b11dbbb0120:patrol/ownership_lock.py.
This is the sole lock implementation in this Wander tree; runtime callers share it.
Keep parity with that reviewed contract. The fixed path is
/tmp/g1-project-locomotion.lock. flock is nonblocking, FD stays open through
returned client lifetime, final STOP attempts and errors until process exit.
Never unlink the inode or delete a stale file. OS release is not physical STOP.
STOP result interpretation and stationary confirmation remain F04 work.

Only cooperating migrated writers on the same host and same lock inode are
covered. Cross-host DDS writers, old deployments and arbitrary services are not.
Forward-distance is not a Phase 1B supported deployment: UNMIGRATED — MUST NOT USE.
Its shared SDK factory now locks, but its CLI/deployment contract is not migrated.

## Offline deployment manifest and next-session checklist

Build with scripts/build-wander-ownership-bundle.py into a local output archive.
It includes the required robot_side lock, runtime, controller, telemetry source,
configuration and package files, plus a manifest with source commit and all file
SHA256 values. No desktop checkout/import is needed by this bundle. The existing
Python 3.8+ SDK environment is provisioned separately; this builder does not install it.
Record the launcher commit from fix/wander-launch-ownership-contract-20260928
beside this manifest. Use the two reviewed commit SHAs, not moving branch names.

Before a future authorized hardware session (not performed in this offline work):

- Confirm old locomotion processes stopped; process exit is not stationary proof.
- Exclude old checkouts, old relay copies and forward-distance from launch targets.
- Install the reviewed source bundle and verify its manifest hashes, including lock.
- Install the matching ownership-required integrated launcher and Phase 1A relay.
- Confirm every participating writer uses the same host/mount/inode and permissions.
- Preserve the lock inode; never remove it to resolve contention.
- Verify CLI contract v1 and explicit operator approval; never downgrade on failure.
- Handle physical stop/transfer confirmation under the separately reviewed F04 plan.
