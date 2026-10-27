"""Suspend detection from Linux clocks, without hooks or a polling worker."""
import time

# Separate clock syscalls have read skew. Ignore sub-millisecond changes rather
# than recording scheduler/read jitter as sleep. This is a measurement bound.
READ_SKEW_NS = 1_000_000


def offset():
    before = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    boot = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    after = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    return boot - (before + after) // 2, after - before


class SleepDetector:
    def __init__(self, read=offset):
        self.read = read
        self.previous, self.skew = read()

    def reconcile(self, collector):
        current, skew = self.read()
        gap = current - self.previous
        if gap <= max(READ_SKEW_NS, skew + self.skew):
            return
        self.previous, self.skew = current, skew
        end = collector.clock()
        # Explicit Omarchy 3 suspend/resume hooks already classify this time.
        if collector.context.suspended:
            return
        # Clock differences determine duration, not the precise suspend instant.
        # Place the gap immediately before discovery, within the uncommitted tail.
        start = max(collector.start, end - gap)
        collector.boundary_at(start, suspended=True)
        collector.boundary_at(end, suspended=False)
