"""Single-command G1 locomotion harness; never connected to WanderDecision."""

from dataclasses import dataclass
import math


ALLOWED_ACTIONS = ('forward', 'turn-left', 'turn-right', 'stop')
MAX_LINEAR_SPEED_M_S = 0.10
MAX_ANGULAR_SPEED_RAD_S = 0.25
MAX_DURATION_S = 0.50
DEFAULT_SPEED = 0.05
DEFAULT_DURATION_S = 0.30


@dataclass(frozen=True)
class OneShotPlan:
    action: str
    speed: float = DEFAULT_SPEED
    duration: float = DEFAULT_DURATION_S

    def validate(self):
        if self.action not in ALLOWED_ACTIONS:
            raise ValueError('action must be one of: ' + ', '.join(ALLOWED_ACTIONS))
        if not math.isfinite(self.speed) or self.speed < 0:
            raise ValueError('speed must be finite and non-negative')
        if not math.isfinite(self.duration) or self.duration <= 0:
            raise ValueError('duration must be finite and positive')
        limit = MAX_LINEAR_SPEED_M_S if self.action == 'forward' else MAX_ANGULAR_SPEED_RAD_S
        if self.action != 'stop' and self.speed > limit:
            raise ValueError('speed exceeds hard limit %.2f for %s' % (limit, self.action))
        if self.duration > MAX_DURATION_S:
            raise ValueError('duration exceeds hard limit %.2f' % MAX_DURATION_S)

    def velocity(self):
        self.validate()
        if self.action == 'forward':
            return self.speed, 0.0, 0.0
        if self.action == 'turn-left':
            return 0.0, 0.0, self.speed
        if self.action == 'turn-right':
            return 0.0, 0.0, -self.speed
        return 0.0, 0.0, 0.0

    def describe(self):
        vx, vy, omega = self.velocity()
        return (
            'action=%s vx=%.3f vy=%.3f omega=%.3f duration=%.3f'
            % (self.action, vx, vy, omega, self.duration)
        )


def execute_once(
    plan,
    runtime,
    interface='eth0',
    timeout=2.0,
    robot='mock',
    enable_real_robot=False,
    execute_real_g1=False,
    understand_motion=False,
):
    """Send exactly one mutation call. A nonzero/unknown result is never retried."""
    plan.validate()
    gates = (robot == 'g1', enable_real_robot, execute_real_g1, understand_motion)
    if not all(gates):
        return {'executed': False, 'result': None, 'description': plan.describe()}
    client = runtime.create_loco_client(interface, timeout)
    if plan.action == 'stop':
        result = client.StopMove()
    else:
        vx, vy, omega = plan.velocity()
        result = client.SetVelocity(vx, vy, omega, plan.duration)
    return {'executed': True, 'result': result, 'description': plan.describe()}
