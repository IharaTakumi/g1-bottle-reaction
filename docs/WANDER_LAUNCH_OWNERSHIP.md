# Wander launch ownership contract v1

The existing explicitly approved launch path now always passes
--require-locomotion-ownership-v1. No F02 approval is inferred or removed.
The production shell launcher already routes Wander through this controller;
no duplicate flag implementation is needed in that shell script.

Legacy Reactive Wander uses argparse.parse_args and fails before SDK init on
this unknown flag. There is no retry without it. An existing process may only
be recognized as RUNNING when its argv contains this exact flag as a token;
unmarked legacy processes are rejected without stopping or replacing them.
This is cooperative version compatibility, not authentication against malicious
processes or proof of stationary state.

Deploy alongside the reviewed fix/wander-locomotion-ownership-20260928 commit.
Record both immutable commit SHAs and verify the Wander bundle manifest, including
robot_side/ownership_lock.py, before a future authorized hardware session.
The canonical path remains /tmp/g1-project-locomotion.lock on the same host/mount.
Exclude old processes/checkouts and unused forward-distance tools from launch.
No hardware deployment is performed by these changes.
