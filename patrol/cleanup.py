"""Attempt every cleanup step before reporting failures."""


class CleanupError(RuntimeError):
    def __init__(self, failures):
        self.failures = tuple(failures)
        super().__init__("cleanup partial failure: " + "; ".join(
            f"{name}: {type(error).__name__}: {error}" for name, error in failures))


def run_cleanup(steps) -> None:
    failures = []
    for name, action in steps:
        try:
            action()
        except BaseException as exc:
            # Even an interrupt during shutdown must not skip later resources.
            failures.append((name, exc))
    if failures:
        raise CleanupError(failures)
