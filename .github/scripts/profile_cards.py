#!/usr/bin/env python3
"""Render streak, activity and typing-banner SVGs from GitHub contributions.

These replace three third-party badge services that rendered at README load time:
a streak counter, an activity graph and a typing banner. Each was a live HTTP
call to someone else's free instance, so the badges broke whenever that instance
was rate limited, cold or shut down. Everything here is computed in CI instead
and published as a static SVG, which GitHub's image proxy serves without ever
reaching an external host.

Output is self-contained SVG animated with CSS keyframes, which is what GitHub
allows in a README (scripts and external refs are stripped, CSS animation is not).
"""

import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

THEMES = {
    "light": {
        "bg": "#ffffff", "text": "#1f2328", "dim": "#59636e",
        "accent": "#2496ED", "flame": "#E36209", "grid": "#d1d9e0",
        "area_from": "#2496ED", "area_to": "#ffffff",
    },
    "dark": {
        "bg": "#1a1b27", "text": "#a9b1d6", "dim": "#565f89",
        "accent": "#2496ED", "flame": "#ff9e64", "grid": "#2c3040",
        "area_from": "#2496ED", "area_to": "#1a1b27",
    },
}

CALENDAR = """query($login:String!,$from:DateTime!,$to:DateTime!){user(login:$login){
contributionsCollection(from:$from,to:$to){contributionCalendar{
totalContributions weeks{contributionDays{date contributionCount}}}}}}"""

CREATED_AT = """query($login:String!){user(login:$login){createdAt}}"""

DAY = datetime.timedelta(days=1)


def graphql(query, variables, token):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": "bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.load(r)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]


def fetch_graphql(user, token):
    """Walk year-long windows from account creation: a calendar query caps at one year."""
    created = graphql(CREATED_AT, {"login": user}, token)["user"]["createdAt"]
    start = datetime.date.fromisoformat(created[:10])
    today = datetime.date.today()
    days = {}
    while start <= today:
        end = min(start + datetime.timedelta(days=364), today)
        data = graphql(CALENDAR, {
            "login": user,
            "from": start.isoformat() + "T00:00:00Z",
            "to": end.isoformat() + "T23:59:59Z",
        }, token)
        weeks = data["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
        for week in weeks:
            for day in week["contributionDays"]:
                days[datetime.date.fromisoformat(day["date"])] = day["contributionCount"]
        start = end + DAY
    return days, created[:10]


def fetch_scrape(user):
    """Public fallback: the contributions fragment needs no authentication.

    Levels alone would give streaks but not totals, so counts come from the
    screen-reader tool-tips, which are keyed to each cell by component id.
    """
    req = urllib.request.Request(
        "https://github.com/users/%s/contributions" % user,
        headers={"User-Agent": "profile-cards-svg"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")

    counts = {}
    for cell, label in re.findall(
        r'for="(contribution-day-component-\d+-\d+)"[^>]*>([^<]*)', html
    ):
        m = re.match(r"(No|[\d,]+) contribution", label.strip())
        if m:
            counts[cell] = 0 if m.group(1) == "No" else int(m.group(1).replace(",", ""))

    days = {}
    for tag in re.findall(r"<td[^>]*ContributionCalendar-day[^>]*>", html):
        date = re.search(r'data-date="(\d{4}-\d{2}-\d{2})"', tag)
        cell = re.search(r'id="(contribution-day-component-\d+-\d+)"', tag)
        if not date:
            continue
        level = re.search(r'data-level="(\d+)"', tag)
        fallback = 1 if level and int(level.group(1)) > 0 else 0
        days[datetime.date.fromisoformat(date.group(1))] = counts.get(
            cell.group(1) if cell else "", fallback
        )
    if not days:
        raise RuntimeError("could not parse contribution calendar for %s" % user)
    return days, min(days).isoformat()


def load_days(user, token):
    if token:
        try:
            return fetch_graphql(user, token)
        except Exception as exc:  # fall through to the public endpoint
            print("graphql failed (%s), falling back to public calendar" % exc, file=sys.stderr)
    return fetch_scrape(user)


def compute_stats(days):
    """Streaks over a contiguous daily calendar (zero days included, so index == day)."""
    dates = sorted(days)
    longest, longest_range, run, run_start = 0, None, 0, None
    for date in dates:
        if days[date] > 0:
            run_start = date if run == 0 else run_start
            run += 1
            if run > longest:
                longest, longest_range = run, (run_start, date)
        else:
            run = 0

    # Today is still in progress, so a blank today must not end the streak.
    i = len(dates) - 1
    if i >= 0 and days[dates[i]] == 0:
        i -= 1
    current, current_range = 0, None
    end = dates[i] if i >= 0 and days[dates[i]] > 0 else None
    while i >= 0 and days[dates[i]] > 0:
        current_range = (dates[i], end)
        current += 1
        i -= 1

    return {
        "total": sum(days.values()),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
        "current": current,
        "current_range": current_range,
        "longest": longest,
        "longest_range": longest_range,
    }


def fmt_day(date, year=False):
    return "%s %d%s" % (date.strftime("%b"), date.day, ", %d" % date.year if year else "")


def fmt_range(pair, years=False):
    if not pair:
        return "—"
    start, end = pair
    years = years or start.year != end.year
    if start == end:
        return fmt_day(start, years)
    return "%s – %s" % (fmt_day(start, years), fmt_day(end, years))


def esc(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"


def svg_open(width, height, label):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
            'viewBox="0 0 %d %d" role="img" aria-label="%s">'
            % (width, height, width, height, esc(label)))


def render_streak(stats, theme_name):
    t = THEMES[theme_name]
    w, h, col = 495, 205, 165
    panels = [
        (str(stats["total"]), "Total Contributions",
         fmt_range((stats["first"], stats["last"]), years=True)
         if stats["first"] else "—", False),
        (str(stats["current"]), "Current Streak", fmt_range(stats["current_range"]), True),
        (str(stats["longest"]), "Longest Streak", fmt_range(stats["longest_range"]), False),
    ]

    parts = [svg_open(w, h, "GitHub contribution streak"),
             '<style>%s</style>' % (
                 "text{%s}" % ("font-family:'Segoe UI',Ubuntu,Helvetica,Arial,sans-serif;"
                               "text-anchor:middle") +
                 # Elements rest in their finished state and the animation only fills
                 # backwards, so a viewer that ignores CSS still renders the card.
                 ".p{animation:fade .6s ease-out backwards}"
                 ".p2{animation-delay:.15s}.p3{animation-delay:.3s}"
                 ".ring{stroke-dasharray:239;"
                 "animation:draw 1.1s .2s ease-out backwards}"
                 "@keyframes fade{from{opacity:0}}@keyframes draw{from{stroke-dashoffset:239}}"),
             '<rect width="%d" height="%d" rx="12" fill="%s"/>' % (w, h, t["bg"])]

    for i, (value, label, sub, ring) in enumerate(panels):
        cx = col * i + col // 2
        cls = "p" if i == 0 else "p p%d" % (i + 1)
        parts.append('<g class="%s">' % cls)
        if ring:
            parts.append(
                '<circle class="ring" cx="%d" cy="90" r="38" fill="none" stroke="%s" '
                'stroke-width="4" stroke-linecap="round" transform="rotate(-90 %d 90)"/>'
                % (cx, t["flame"], cx))
            parts.append(
                '<path transform="translate(%d 30)" fill="%s" d="M1 -16'
                'c1 6 6 8 6 14c0 3 -1 5 -3 6c1 -4 -1 -7 -3 -8c1 4 -1 6 -3 8'
                'c-2 -2 -2 -5 -1 -8c-3 2 -4 6 -4 9a9 9 0 0 0 18 0'
                'c0 -8 -6 -14 -10 -21z"/>' % (cx, t["flame"]))
        parts.append('<text x="%d" y="100" font-size="30" font-weight="700" fill="%s">%s</text>'
                     % (cx, t["text"], esc(value)))
        parts.append('<text x="%d" y="147" font-size="13" font-weight="600" fill="%s">%s</text>'
                     % (cx, t["accent"] if ring else t["text"], esc(label.upper())))
        parts.append('<text x="%d" y="168" font-size="11" fill="%s">%s</text>'
                     % (cx, t["dim"], esc(sub)))
        parts.append("</g>")
        if i:
            parts.append('<line x1="%d" y1="60" x2="%d" y2="175" stroke="%s" stroke-width="1"/>'
                         % (col * i, col * i, t["grid"]))

    parts.append("</svg>")
    return "".join(parts) + "\n"


def weekly_buckets(days, weeks_back):
    dates = sorted(days)
    end = dates[-1]
    start = end - datetime.timedelta(days=weeks_back * 7 - 1)
    buckets, cursor = [], start
    while cursor <= end:
        stop = min(cursor + datetime.timedelta(days=6), end)
        total, day = 0, cursor
        while day <= stop:
            total += days.get(day, 0)
            day += DAY
        buckets.append((cursor, total))
        cursor = stop + DAY
    return buckets


def nice_ceiling(value):
    """Round the y-axis up to 1/2/5 x a power of ten so the labels stay readable."""
    if value <= 4:
        return max(value, 1)
    step = 10 ** (len(str(int(value))) - 1)
    for mult in (1, 2, 5, 10):
        if value <= mult * step:
            return mult * step
    return 10 * step


def render_activity(days, theme_name, weeks_back):
    t = THEMES[theme_name]
    w, h = 820, 320
    left, right, top, bottom = 52, 24, 52, 48
    plot_w, plot_h = w - left - right, h - top - bottom
    buckets = weekly_buckets(days, weeks_back)
    ymax = nice_ceiling(max([b[1] for b in buckets] + [1]))
    base = top + plot_h

    def px(i):
        return left + (plot_w * i / (len(buckets) - 1.0) if len(buckets) > 1 else 0)

    def py(value):
        return base - plot_h * (value / float(ymax))

    points = [(px(i), py(v)) for i, (_, v) in enumerate(buckets)]
    line = "M" + " L".join("%.1f %.1f" % p for p in points)
    area = "%s L%.1f %.1f L%.1f %.1f Z" % (line, points[-1][0], base, points[0][0], base)
    length = sum(
        ((points[i + 1][0] - points[i][0]) ** 2 + (points[i + 1][1] - points[i][1]) ** 2) ** 0.5
        for i in range(len(points) - 1)
    ) or 1

    parts = [
        svg_open(w, h, "Weekly contribution activity"),
        "<style>"
        "text{font-family:'Segoe UI',Ubuntu,Helvetica,Arial,sans-serif}"
        ".ln{stroke-dasharray:%.0f;animation:draw 1.6s ease-out backwards}"
        ".ar{animation:fade .9s .5s ease-out backwards}"
        ".dt{animation:fade .5s .9s ease-out backwards}"
        "@keyframes draw{from{stroke-dashoffset:%.0f}}@keyframes fade{from{opacity:0}}"
        "</style>" % (length, length),
        '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%%" stop-color="%s" stop-opacity=".45"/>'
        '<stop offset="100%%" stop-color="%s" stop-opacity="0"/>'
        "</linearGradient></defs>" % (t["area_from"], t["area_from"]),
        '<rect width="%d" height="%d" rx="12" fill="%s"/>' % (w, h, t["bg"]),
        '<text x="24" y="32" font-size="15" font-weight="700" fill="%s">'
        "Contribution activity</text>" % t["text"],
        '<text x="%d" y="32" font-size="12" text-anchor="end" fill="%s">'
        "last %d weeks</text>" % (w - 24, t["dim"], len(buckets)),
    ]

    for step in range(5):
        value = ymax * step / 4.0
        y = py(value)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                     % (left, y, w - right, y, t["grid"]))
        parts.append('<text x="%d" y="%.1f" font-size="11" text-anchor="end" fill="%s">%d</text>'
                     % (left - 10, y + 4, t["dim"], round(value)))

    seen, last_x = set(), -1e9
    for i, (date, _) in enumerate(buckets):
        key = (date.year, date.month)
        if key in seen or px(i) - last_x < 42 or px(i) > w - right - 18:
            continue
        seen.add(key)
        last_x = px(i)
        parts.append('<text x="%.1f" y="%d" font-size="11" text-anchor="middle" fill="%s">%s</text>'
                     % (px(i), base + 24, t["dim"], date.strftime("%b")))

    parts.append('<path class="ar" d="%s" fill="url(#g)"/>' % area)
    parts.append('<path class="ln" d="%s" fill="none" stroke="%s" stroke-width="2.5" '
                 'stroke-linejoin="round" stroke-linecap="round"/>' % (line, t["accent"]))
    for (x, y), (_, value) in zip(points, buckets):
        if value:
            parts.append('<circle class="dt" cx="%.1f" cy="%.1f" r="3.5" fill="%s" '
                         'stroke="%s" stroke-width="2"/>' % (x, y, t["accent"], t["bg"]))
    parts.append("</svg>")
    return "".join(parts) + "\n"


FONT_SIZE = 22
# Upper bound on the advance of a monospace glyph at FONT_SIZE. textLength pins
# the line to exactly this, and a renderer that ignores textLength draws
# narrower than the clip rather than losing the last characters.
CHAR_W = 13.8
BANNER_H = 54
BASELINE = 36


def render_typing(lines, theme_name, type_speed, hold):
    """Typewriter reveal built from a stepped clip mask.

    `textLength` pins each line to an exact width, so the caret lands on the same
    pixel as the clip edge no matter which monospace face the viewer resolves.
    """
    t = THEMES[theme_name]
    width = max(680, int(max(len(s) for s in lines) * CHAR_W) + 48)
    spans, cursor = [], 0.0
    for text in lines:
        typing = len(text) * type_speed
        spans.append((cursor, cursor + typing, cursor + typing + hold))
        cursor += typing + hold
    total = cursor

    def pct(seconds):
        return round(seconds / total * 100, 3)

    css = ["text{font-family:%s;font-weight:600}" % MONO,
           "@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}"]
    clips, groups = [], []

    for i, text in enumerate(lines):
        n = len(text)
        tl = n * CHAR_W
        x = (width - tl) / 2.0
        start, typed, end = (pct(s) for s in spans[i])
        steps = "animation-timing-function:steps(%d)" % n

        def frames(off_hidden, off_shown):
            out = [f"0%{{transform:translateX({off_hidden}px)"
                   + (f";{steps}}}" if start == 0 else "}")]
            if start > 0:
                out.append(f"{start}%{{transform:translateX({off_hidden}px);{steps}}}")
            out.append(f"{typed}%{{transform:translateX({off_shown}px)}}")
            out.append(f"{end}%{{transform:translateX({off_shown}px)}}")
            out.append(f"100%{{transform:translateX({off_hidden}px)}}")
            return "".join(out)

        css.append("@keyframes m%d{%s}" % (i, frames(round(-tl, 2), 0)))
        css.append("@keyframes k%d{%s}" % (i, frames(0, round(tl, 2))))
        visible = ("0%{opacity:1}" if start == 0 else "0%%{opacity:0}%s%%{opacity:1}" % start)
        css.append("@keyframes o%d{%s%s%%{opacity:0}100%%{opacity:0}}" % (i, visible, end))
        css.append(".o%d{opacity:%d;animation:o%d %.2fs infinite step-end}"
                   % (i, 1 if i == 0 else 0, i, total))
        css.append(".m%d{animation:m%d %.2fs infinite}" % (i, i, total))
        css.append(".k%d{animation:k%d %.2fs infinite,blink 1s infinite}" % (i, i, total))

        clips.append('<clipPath id="c%d"><rect class="m%d" x="%.2f" y="0" width="%.2f" '
                     'height="%d"/></clipPath>' % (i, i, x, tl, BANNER_H))
        groups.append(
            '<g class="o%d"><g clip-path="url(#c%d)"><text x="%.2f" y="%d" font-size="%d" '
            'textLength="%.2f" lengthAdjust="spacingAndGlyphs" fill="%s">%s</text></g>'
            '<rect class="k%d" x="%.2f" y="%d" width="2" height="26" fill="%s"/></g>'
            % (i, i, x, BASELINE, FONT_SIZE, tl, t["accent"], esc(text),
               i, x, BASELINE - 20, t["accent"]))

    return ("%s<style>%s</style><defs>%s</defs>%s</svg>\n"
            % (svg_open(width, BANNER_H, "; ".join(lines)),
               "".join(css), "".join(clips), "".join(groups)))


DEFAULT_LINES = [
    "DevOps & Systems Engineer",
    "On-prem infra \u2014 Proxmox, Kubernetes, Coolify",
    "Networking, VPN & reverse proxies that stay up",
    "Automation, CI/CD & observability",
    "Backend across Python, Java, C# & PHP",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--activity-weeks", type=int, default=52)
    ap.add_argument("--type-speed", type=float, default=0.06,
                    help="seconds per character in the typing banner")
    ap.add_argument("--hold", type=float, default=1.8,
                    help="seconds a fully typed line stays on screen")
    ap.add_argument("--line", action="append", dest="lines",
                    help="typing banner line (repeatable; replaces the defaults)")
    ap.add_argument("--out-dir", default="dist")
    args = ap.parse_args()

    days, created = load_days(args.user, args.token)
    stats = compute_stats(days)
    print("calendar since %s | %d days | %d contributions | current %d | longest %d"
          % (created, len(days), stats["total"], stats["current"], stats["longest"]),
          file=sys.stderr)

    lines = args.lines or DEFAULT_LINES
    os.makedirs(args.out_dir, exist_ok=True)
    cards = {}
    for theme, suffix in (("light", ""), ("dark", "-dark")):
        cards["streak%s.svg" % suffix] = render_streak(stats, theme)
        cards["activity%s.svg" % suffix] = render_activity(days, theme, args.activity_weeks)
    # The banner is transparent and drawn in the brand accent, which reads on both
    # backgrounds, so it needs no per-theme variant.
    cards["typing.svg"] = render_typing(lines, "dark", args.type_speed, args.hold)

    for name, svg in sorted(cards.items()):
        path = os.path.join(args.out_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg)
        print("wrote %s (%d bytes)" % (path, os.path.getsize(path)), file=sys.stderr)


if __name__ == "__main__":
    main()
