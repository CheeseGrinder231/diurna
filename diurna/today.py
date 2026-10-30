"""Read-only rollup of stored spans for one local day. No writes, no titles invented."""
from datetime import datetime, time as clock_time, timedelta
import heapq
from itertools import groupby
from pathlib import Path
import sqlite3
import time

from .config import policy

REASONS = ("active", "idle", "locked", "suspended", "disconnected", "unknown")
# Active time split by the stored signals; parts add up to active time.
ACTIVITIES = ("hands_on", "watching", "on_screen")
SECOND = 1_000_000_000
SCREENSAVER = "org.omarchy.screensaver"
# The config default of `screensaver_grace_seconds`; `today` reads the setting.
SCREENSAVER_GRACE_NS = 10 * SECOND
# Nobody is there. Once a stretch is away, only input ends it (`classify`).
AWAY = ("idle", "locked", "suspended")
# A stretch of focus shorter than this is a switch, not an activity of its own:
# it folds into a neighbouring row. See `timeline`.
FOLD_NS = 3 * 60 * SECOND


def day_bounds(now_ns):
    """Local midnight to the next local midnight, via mktime so DST days are exact."""
    day = datetime.fromtimestamp(now_ns // SECOND).date()
    start = time.mktime(datetime.combine(day, clock_time()).timetuple())
    end = time.mktime(datetime.combine(day + timedelta(days=1), clock_time()).timetuple())
    return day, int(start) * SECOND, int(end) * SECOND


def rows(database, start_ns, end_ns):
    """Spans overlapping [start_ns, end_ns), as (start, end, app, workspace,
    reason, title, input, media, inhibiting); signals are None before version 2.

    Whether time after start_ns is still away depends on earlier spans, so the
    result starts at the last span that settles it (`classify`): input, active
    time without signals, or unknown time. Readers clip to the range.
    """
    path = Path(database)
    if not path.exists():
        return []
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, 2, 3):
            raise ValueError("unsupported diurna schema")
        if version == 1:
            return db.execute("""
                SELECT start_ns,end_ns,app_class,workspace,reason,title,NULL,NULL,NULL
                FROM spans WHERE end_ns > ? AND start_ns < ? ORDER BY start_ns
                """, (start_ns, end_ns)).fetchall()
        anchor = db.execute("""
            SELECT start_ns FROM spans WHERE start_ns < ? AND (input = 1
                OR (input IS NULL AND reason = 'active' AND app_class != ?)
                OR reason IN ('unknown', 'disconnected'))
            ORDER BY start_ns DESC LIMIT 1""", (start_ns, SCREENSAVER)).fetchone()
        if anchor is None:
            anchor = db.execute("SELECT MIN(start_ns) FROM spans").fetchone()
        first = start_ns if anchor[0] is None else min(anchor[0], start_ns)
        return db.execute("""
            SELECT start_ns,end_ns,app_class,workspace,reason,title,input,media,inhibiting
            FROM spans WHERE end_ns > ? AND start_ns < ? ORDER BY start_ns
            """, (first, end_ns)).fetchall()
    finally:
        db.close()


def _reported_reason(app, reason):
    # The fullscreen screensaver can inhibit the desktop's idle monitor. Apply
    # this at read time so existing active spans become idle without rewriting
    # history. Preserve explicit lock, sleep and collector-unavailable states.
    if reason == "active" and app == SCREENSAVER:
        return "idle"
    return reason


def _activity(input_, media, inhibiting):
    """Hands-on with input; watching when the focused app's player is Playing,
    or it has no player and its window holds an idle inhibitor; else on screen.
    None without an input signal."""
    if input_ is None:
        return None
    if input_:
        return "hands_on"
    return "watching" if media or (media is None and inhibiting) else "on_screen"


def classify(spans, grace_ns=SCREENSAVER_GRACE_NS):
    """Spans as [start, end, app, workspace, reason, title, activity, away].

    `reason` is the reported reason. Active spans have an `activity`, or None
    when recorded without signals (before the split, or without the shell
    plugin); those read exactly as before. `away` marks active time read as
    idle by two rules over the raw spans, so changing a rule rewrites nothing:

    - An on-screen stretch that runs into the screensaver, with only on-screen
      or idle time and no input between, is away if the screensaver stays up
      longer than `grace_ns`. Dismissed sooner, the user was there.
    - Leaving away needs input. After idle, locked, asleep or screensaver time,
      watching and on-screen time stay away until input: a focus change (the
      lock screen leaves no window focused) or an inhibitor ending does not
      end it. A gap or unknown time, or a span without signals, also ends it.
    """
    signals = [tuple(span[6:9]) + (None,) * (3 - len(span[6:9])) for span in spans]
    out = []
    for (start, end, app, workspace, reason, title, *_), (input_, media, inhibiting) \
            in zip(spans, signals):
        reason = _reported_reason(app, reason)
        activity = _activity(input_, media, inhibiting) if reason == "active" else None
        out.append([start, end, app, workspace, reason, title, activity, False])

    def away(row):
        row[4], row[6], row[7] = "idle", None, True

    index = 0
    while index < len(out):
        if out[index][2] != SCREENSAVER:
            index += 1
            continue
        first = index
        while index + 1 < len(out) and out[index + 1][2] == SCREENSAVER \
                and out[index + 1][0] == out[index][1]:
            index += 1
        if out[index][1] - out[first][0] > grace_ns:
            before = first - 1
            while before >= 0 and out[before][1] == out[before + 1][0] \
                    and signals[before][0] != 1:
                if out[before][6] == "on_screen":
                    away(out[before])
                elif out[before][4] != "idle":
                    break
                before -= 1
        index += 1
    gone, previous = False, None
    for row, (input_, _, _) in zip(out, signals):
        if row[0] != previous:
            gone = False
        previous = row[1]
        if gone and row[6] in ("watching", "on_screen"):
            away(row)
        if input_:
            gone = False
        elif row[4] in AWAY or row[2] == SCREENSAVER:
            gone = True
        elif row[4] in ("unknown", "disconnected") or (row[4] == "active" and row[6] is None):
            gone = False
    return out


def _split(totals):
    totals = totals or {}
    return {f"{activity}_ns": totals.get(activity, 0) for activity in ACTIVITIES}


def summarize(spans, start_ns, end_ns, date=None, grace_ns=SCREENSAVER_GRACE_NS):
    """Clip spans to [start_ns, end_ns) and total them by state, app and window.

    A window is an app on a workspace, plus its title when a privacy rule kept one;
    titles are never stored until per-app rules exist, so title is null today.
    Active time is split into hands-on, watching and on screen (`classify`);
    `unsplit_ns` is active time recorded without signals. `away_ns` is time the
    screensaver and away rules took from active; it is part of idle.
    """
    classified = classify(spans, grace_ns)
    states = dict.fromkeys(REASONS, 0)
    activities = dict.fromkeys(ACTIVITIES, 0)
    apps, windows, splits = {}, {}, {}
    away = 0
    last_end = start_ns
    for start, end, app, workspace, reason, title, activity, moved in classified:
        last_end = max(last_end, min(end, end_ns))
        duration = min(end, end_ns) - max(start, start_ns)
        if duration <= 0:
            continue
        states[reason] += duration
        if moved:
            away += duration
        if reason == "active":
            apps[app] = apps.get(app, 0) + duration
            key = (app, workspace, title)
            windows[key] = windows.get(key, 0) + duration
            if activity:
                activities[activity] += duration
                split = splits.setdefault(app, {})
                split[activity] = split.get(activity, 0) + duration

    def ranked(totals):
        return sorted(totals.items(), key=lambda item: (-item[1], item[0]))

    return {
        "date": date.isoformat() if date else None,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "active_ns": states["active"],
        "activity": {**_split(activities),
                     "unsplit_ns": states["active"] - sum(activities.values())},
        "away_ns": away,
        "states": {f"{reason}_ns": value for reason, value in states.items()},
        # Before the first span or between spans: no collector wrote anything.
        "not_recorded_ns": last_end - start_ns - sum(states.values()),
        # After the last committed span: the collector's open span while it
        # runs, unrecorded time if it stopped.
        "pending_ns": end_ns - last_end,
        "apps": [{"app_class": app, "active_ns": value, **_split(splits.get(app))}
                 for app, value in ranked(apps)],
        "windows": [{"app_class": app, "workspace": workspace, "title": title,
                     "active_ns": value}
                    for (app, workspace, title), value in sorted(
                        windows.items(), key=lambda item: (-item[1], tuple(
                            "" if part is None else part for part in item[0])))],
        "timeline": _timeline(classified, start_ns, end_ns),
    }


def timeline(spans, start_ns, end_ns, grace_ns=SCREENSAVER_GRACE_NS):
    """Rows that tile [start_ns, end_ns) exactly: from when to when, what.

    States are the reported reasons, plus `not_recorded` (no span: Diurna was not
    running) and `pending` (after the last committed span). Each non-active state
    is its own row. Within active time, focus shorter than FOLD_NS is a switch,
    not an activity: it folds into a neighbouring row (`_fold`). A row is named by
    the app and workspace holding most of its time, or is mixed (app_class null)
    when none does. It counts its `switches` and lists each (app, workspace) in it.
    `activity` totals its hands-on, watching and on-screen time; a change of
    activity within one app is not a switch.
    """
    return _timeline(classify(spans, grace_ns), start_ns, end_ns)


def _timeline(classified, start_ns, end_ns):
    pieces, cursor = [], start_ns
    for start, end, app, workspace, reason, _, activity, _ in classified:
        start, end = max(start, cursor), min(end, end_ns)
        if end <= start:
            continue
        if start > cursor:
            pieces.append([cursor, start, "not_recorded", None, {}])
        key = (app, workspace) if reason == "active" else None
        if pieces and pieces[-1][1] == start and pieces[-1][2:4] == [reason, key]:
            pieces[-1][1] = end  # read-side merge of rows split by no-op events
        else:
            pieces.append([start, end, reason, key, {}])
        if activity:
            split = pieces[-1][4]
            split[activity] = split.get(activity, 0) + end - start
        cursor = end
    if cursor < end_ns:
        pieces.append([cursor, end_ns, "pending", None, {}])
    rows = []
    for state, group in groupby(pieces, key=lambda piece: piece[2]):
        group = list(group)
        if state == "active":
            rows += [_row(state, row) for row in _fold(
                [[start, end, {key: end - start}, 1, split]
                 for start, end, _, key, split in group])]
        else:
            rows += [_row(state, [start, end, {}, 1, {}]) for start, end, *_ in group]
    return rows


def settle(timeline):
    """Timeline rows for the tabbed view: no row shorter than FOLD_NS, when it has
    a neighbour to join.

    `timeline` keeps each idle, locked or unsaved stretch as its own row however
    short, so a sub-second idle blip prints as `0s idle`. Here every row is a
    part keyed (state, app, workspace) and the same fold applies across states.
    A row is named by the part holding most of its time (`state`, `app_class`,
    `workspace`), or is mixed (all three None). `parts` lists every part.
    """
    rows = []
    for row in timeline:
        if row["state"] == "active":
            parts = {("active", a["app_class"], a["workspace"]): a["active_ns"]
                     for a in row["apps"]}
        else:
            parts = {(row["state"], None, None): row["end_ns"] - row["start_ns"]}
        split = {activity: row["activity"][f"{activity}_ns"] for activity in ACTIVITIES}
        rows.append([row["start_ns"], row["end_ns"], parts, row["switches"] + 1, split])
    settled = []
    for row in _fold(rows) if rows else []:
        start, end, parts, segments, split = row
        state, app, workspace = _key(row) or (None, None, None)
        settled.append({
            "start_ns": start, "end_ns": end, "state": state, "app_class": app,
            "workspace": workspace, "switches": segments - 1, "activity": _split(split),
            "parts": [{"state": s, "app_class": a, "workspace": w, "ns": value}
                      for (s, a, w), value in sorted(parts.items(), key=lambda item: (
                          -item[1], tuple("" if part is None else part
                                          for part in item[0])))],
        })
    return settled


def _fold(rows):
    """Fold rows [start, end, {key: ns}, segments, {activity: ns}] of one run;
    keys are (app, workspace) in active time.

    The shortest row under FOLD_NS joins a neighbour, until none is left or the
    run is one row: all three when both neighbours are the same app, else the
    shorter neighbour. Shortest first, so sub-second blips (a file picker, a
    popup) rejoin the stay they interrupted before short stays are weighed. The
    shorter neighbour, so a burst of switching gathers into its own mixed row
    rather than vanishing into the stay before it. Adjacent rows with the same
    app, or both mixed, then join.
    """
    before = list(range(-1, len(rows) - 1))
    after = list(range(1, len(rows) + 1))
    after[-1] = -1
    heap = [(end - start, start, index) for index, (start, end, *_) in enumerate(rows)]
    heapq.heapify(heap)
    while heap:
        length, _, index = heapq.heappop(heap)
        row = rows[index]
        if row is None or length != row[1] - row[0]:
            continue  # merged away or grown since this entry
        if length >= FOLD_NS:
            break
        previous, following = before[index], after[index]
        if previous < 0 and following < 0:
            break
        if previous >= 0 and following >= 0 and _key(rows[previous]) is not None \
                and _key(rows[previous]) == _key(rows[following]):
            target, absorbed = previous, (index, following)
        elif following < 0 or (previous >= 0 and _length(rows[previous])
                               <= _length(rows[following])):
            target, absorbed = previous, (index,)
        else:
            target, absorbed = index, (following,)
        for gone in absorbed:
            _join(rows[target], rows[gone])
            rows[gone] = None
            after[target] = after[gone]
            if after[target] >= 0:
                before[after[target]] = target
        heapq.heappush(heap, (_length(rows[target]), rows[target][0], target))
    merged = []
    for row in filter(None, rows):
        if merged and _key(merged[-1]) == _key(row):
            _join(merged[-1], row)
        else:
            merged.append(row)
    return merged


def _length(row):
    return row[1] - row[0]


def _join(row, following):
    row[1] = following[1]
    for key, value in following[2].items():
        row[2][key] = row[2].get(key, 0) + value
    row[3] += following[3]
    for activity, value in following[4].items():
        row[4][activity] = row[4].get(activity, 0) + value


def _key(row):
    """The key holding most of the row's time, or None if none does."""
    if not row[2]:
        return None
    key, value = min(row[2].items(), key=lambda item: (
        -item[1], tuple("" if part is None else part for part in item[0])))
    return key if 2 * value > _length(row) else None


def _row(state, row):
    start, end, apps, segments, split = row
    key = _key(row)
    return {
        "start_ns": start, "end_ns": end, "state": state,
        "app_class": key[0] if key else None, "workspace": key[1] if key else None,
        # Focus changes inside the row; adjacent segments always differ.
        "switches": segments - 1,
        "activity": _split(split),
        "apps": [{"app_class": app, "workspace": workspace, "active_ns": value}
                 for (app, workspace), value in sorted(
                     apps.items(), key=lambda item: (-item[1], item[0]))],
    }


def today(database, now_ns):
    date, start_ns, _ = day_bounds(now_ns)
    grace_ns = int(policy()["screensaver_grace_seconds"] * SECOND)
    return summarize(rows(database, start_ns, now_ns), start_ns, now_ns, date, grace_ns)
