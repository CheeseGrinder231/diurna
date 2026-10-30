"""Frozen export classifier. Independent of settings and product view code.

Input is the stored view tuple (start, end, app, workspace, reason, title,
input, media, inhibiting). Callers validate source history before classification.
"""
RULE = "diurna-screen-use-app-fold-180s-v1"
SCREENSAVER = "org.omarchy.screensaver"
GRACE_NS = 10_000_000_000
AWAY = ("idle", "locked", "suspended")


def classify(spans):
    """Return [start, end, app, workspace, reason, title, activity, moved].

    The ordered passes are frozen in docs/screen-use.md, including exceptional
    screensaver flags. Never import today's classifier or its hot configuration.
    """
    signals = [tuple(span[6:9]) + (None,) * (3 - len(span[6:9])) for span in spans]
    out = []
    for (start, end, app, workspace, reason, title, *_), (input_, media, inhibiting) \
            in zip(spans, signals):
        if reason == "active" and app == SCREENSAVER:
            reason = "idle"
        activity = None
        if reason == "active" and input_ is not None:
            activity = "hands_on" if input_ else (
                "watching" if media or (media is None and inhibiting) else "on_screen")
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
        if out[index][1] - out[first][0] > GRACE_NS:
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


def barriers(spans):
    """Only committed evidence. Silence, inactivity, and policy cuts do not seal.

    Entries are (watermark, kind, previous_end, barrier_end). A proved positive
    gap uses the later span's start, even when that span is a screensaver.
    """
    evidence, previous = [], None
    for start, end, app, _, reason, _, *signals in spans:
        input_ = signals[0] if signals else None
        if previous is not None and start > previous:
            evidence.append((start, "gap", previous, end))
        if app != SCREENSAVER:
            kind = ("input" if input_ == 1 else
                    "unknown" if reason in ("unknown", "disconnected") else
                    "legacy" if reason == "active" and input_ is None else None)
            if kind:
                evidence.append((start, kind, previous, end))
        previous = end
    return evidence
