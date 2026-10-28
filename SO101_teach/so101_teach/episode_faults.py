"""Only visual decisions/timeouts may be retried without ending an episode."""


class RetryableFault(Exception):
    pass


class InspectionRetry(RetryableFault,ValueError):
    pass


class JigRetry(RetryableFault,RuntimeError):
    pass


class ResumeBlocked(RetryableFault,RuntimeError):
    """Current evidence cannot authorize motion; remain paused for an operator."""


class RecheckInterrupted(Exception):
    """A newer stop invalidated the read-only retry attempt."""
