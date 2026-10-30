"""Frozen app-only grouping of complete, classifier-sealed policy domains."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .screen_classifier import classify

SECOND = 1_000_000_000
DAY_NS = 86_400 * SECOND
FOLD_NS = 180 * SECOND
CALENDAR = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class Period:
    start_ns: int
    end_ns: int
    application: str | None
    # Exact UTF-8 order is part of the evidence, never exposed as attributes.
    apps: tuple
    domain_start_ns: int
    closure_ns: int
    closure_kind: str


def _label(row):
    app, duration = min(row[2].items(), key=lambda pair: (-pair[1], pair[0].encode("utf-8")))
    return app if duration * 2 > row[1] - row[0] else None


def _join(left, right):
    left[1] = right[1]
    for app, duration in right[2].items():
        left[2][app] = left[2].get(app, 0) + duration


def fold(rows):
    """Shortest-first, earliest-start ties. A bounded replay caps quadratic work."""
    rows = [[start, end, dict(apps)] for start, end, apps in rows]
    while len(rows) > 1:
        index = min(range(len(rows)), key=lambda i: (rows[i][1] - rows[i][0], rows[i][0]))
        if rows[index][1] - rows[index][0] >= FOLD_NS:
            break
        if 0 < index < len(rows) - 1 and _label(rows[index - 1]) is not None \
                and _label(rows[index - 1]) == _label(rows[index + 1]):
            _join(rows[index - 1], rows[index])
            _join(rows[index - 1], rows[index + 1])
            del rows[index:index + 2]
        elif index == len(rows) - 1 or (index > 0 and
                rows[index - 1][1] - rows[index - 1][0]
                <= rows[index + 1][1] - rows[index + 1][0]):
            _join(rows[index - 1], rows[index])
            del rows[index]
        else:
            _join(rows[index], rows[index + 1])
            del rows[index + 1]
    merged = []
    for row in rows:
        if merged and _label(merged[-1]) == _label(row):
            _join(merged[-1], row)
        else:
            merged.append(row)
    return merged


def _cut(start):
    instant = datetime.fromtimestamp(start // SECOND, timezone.utc).astimezone(CALENDAR)
    midnight = datetime.combine(instant.date() + timedelta(days=1), time(), CALENDAR)
    # datetime is used only at integral seconds; nanoseconds stay integers.
    seconds = (midnight.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc))
    next_day = (seconds.days * 86400 + seconds.seconds) * SECOND
    return min(next_day, start + DAY_NS)


def periods(spans, watermark, max_domains=4096):
    """Use complete source context; return only domains closed at/before W.

    An active run ends at classified inactivity, a proved gap, or a covered
    policy cut. A barrier inside the run cannot split it. The source tail is
    always open except when it covers a policy cut exactly.
    """
    result, rows = [], []
    domains = 0
    domain_start = cut = previous = None

    def finish(closure, kind):
        nonlocal rows, domain_start, cut, domains
        if rows:
            domains += 1
            if domains > max_domains:
                raise ValueError("grouping domain budget")
        if rows and watermark is not None and closure <= watermark:
            for start, end, apps in fold(rows):
                result.append(Period(start, end, _label([start, end, apps]),
                                     tuple(sorted(apps.items(), key=lambda p: p[0].encode("utf-8"))),
                                     domain_start, closure, kind))
                if len(result) > max_domains:
                    raise ValueError("period row budget")
        rows, domain_start, cut = [], None, None

    for start, end, app, _, reason, *_ in classify(spans):
        if previous is not None and start != previous:
            finish(previous, "gap")
        if reason != "active":
            finish(start, "inactive")
        else:
            cursor = start
            while cursor < end:
                if not rows:
                    domain_start = cursor
                    cut = _cut(cursor)
                stop = min(end, cut)
                if rows and rows[-1][1] == cursor and list(rows[-1][2]) == [app]:
                    rows[-1][1] = stop
                    rows[-1][2][app] += stop - cursor
                else:
                    rows.append([cursor, stop, {app: stop - cursor}])
                cursor = stop
                if cursor == cut:
                    finish(cut, "policy")
        previous = end
    return result
