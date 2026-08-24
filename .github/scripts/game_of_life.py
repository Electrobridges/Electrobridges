#!/usr/bin/env python3
"""Render a GitHub contribution graph as an animated Conway's Game of Life SVG.

The contribution grid seeds generation 0. The board is toroidal (edges wrap), so
patterns travel across the graph instead of dying against the borders. Sparse
graphs get extra seed patterns injected so the animation actually evolves.

Output is a self-contained SVG animated with CSS keyframes, which is what GitHub
allows in a README (scripts and external refs are stripped, CSS animation is not).
"""

import argparse
import json
import random
import re
import sys
import urllib.error
import urllib.request

ROWS = 7
PITCH = 13
CELL = 10
PAD = 10

THEMES = {
    "light": {"empty": "#ebedf0", "ages": ["#9be9a8", "#40c463", "#30a14e", "#216e39"]},
    "dark": {"empty": "#161b22", "ages": ["#0e4429", "#006d32", "#26a641", "#39d353"]},
}

GRAPHQL = """query($login:String!){user(login:$login){contributionsCollection{
contributionCalendar{weeks{contributionDays{contributionLevel weekday}}}}}}"""

LEVELS = {
    "NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2,
    "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4,
}


def fetch_graphql(user, token):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": GRAPHQL, "variables": {"login": user}}).encode(),
        headers={"Authorization": "bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.load(r)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    weeks = payload["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
    grid = {}
    for col, week in enumerate(weeks):
        for day in week["contributionDays"]:
            grid[(day["weekday"], col)] = LEVELS.get(day["contributionLevel"], 0)
    return grid, len(weeks)


def fetch_scrape(user):
    """Public fallback: the contributions fragment needs no authentication."""
    req = urllib.request.Request(
        "https://github.com/users/%s/contributions" % user,
        headers={"User-Agent": "game-of-life-svg"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")
    grid, cols = {}, 0
    pattern = re.compile(
        r'id="contribution-day-component-(\d+)-(\d+)"[^>]*data-level="(\d+)"'
    )
    # Attribute order is not guaranteed, so try both orderings.
    alt = re.compile(
        r'data-level="(\d+)"[^>]*id="contribution-day-component-(\d+)-(\d+)"'
    )
    for tag in re.findall(r"<td[^>]*ContributionCalendar-day[^>]*>", html):
        m = pattern.search(tag)
        if m:
            row, col, lvl = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = alt.search(tag)
            if not m:
                continue
            lvl, row, col = int(m.group(1)), int(m.group(2)), int(m.group(3))
        grid[(row, col)] = lvl
        cols = max(cols, col + 1)
    if not grid:
        raise RuntimeError("could not parse contribution grid for %s" % user)
    return grid, cols


def load_grid(user, token):
    if token:
        try:
            return fetch_graphql(user, token)
        except Exception as exc:  # fall through to the public endpoint
            print("graphql failed (%s), falling back to public grid" % exc, file=sys.stderr)
    return fetch_scrape(user)


GLIDER = [(0, 1), (1, 2), (2, 0), (2, 1), (2, 2)]
BLINKER = [(0, 0), (0, 1), (0, 2)]
R_PENTOMINO = [(0, 1), (0, 2), (1, 0), (1, 1), (2, 1)]


def stamp(alive, shape, row, col, cols):
    for dr, dc in shape:
        alive.add(((row + dr) % ROWS, (col + dc) % cols))


def build_seed(grid, cols, min_cells, rng):
    alive = {pos for pos, lvl in grid.items() if lvl > 0}
    seeded = len(alive)
    while len(alive) < min_cells:
        stamp(alive, rng.choice(SHAPES), rng.randrange(ROWS), rng.randrange(cols), cols)
    return alive, seeded


def step(alive, cols):
    counts = {}
    for row, col in alive:
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr or dc:
                    key = ((row + dr) % ROWS, (col + dc) % cols)
                    counts[key] = counts.get(key, 0) + 1
    return {
        pos for pos, n in counts.items()
        if n == 3 or (n == 2 and pos in alive)
    }


SHAPES = [GLIDER, R_PENTOMINO, BLINKER]


def simulate(alive, cols, generations, rng, min_cells):
    """Yield per-generation age maps (cell -> consecutive generations alive).

    A 53x7 torus is small enough that Life collapses within ~50 generations,
    either into still lifes and blinkers or down to a lone glider circling
    forever. Both look dead on a README, so the board is topped up whenever it
    repeats a recent state or drops below a population floor.
    """
    floor = max(18, min_cells // 3)
    target = max(floor + 8, min_cells // 2)
    ages = {pos: 1 for pos in alive}
    history = [dict(ages)]
    recent = [frozenset(alive)]
    for _ in range(generations - 1):
        alive = step(alive, cols)
        stalled = len(recent) >= 2 and frozenset(alive) in (recent[-1], recent[-2])
        if stalled or len(alive) < floor:
            for _ in range(24):
                if len(alive) >= target:
                    break
                stamp(alive, rng.choice(SHAPES), rng.randrange(ROWS), rng.randrange(cols), cols)
        ages = {pos: min(ages.get(pos, 0) + 1, 4) for pos in alive}
        history.append(dict(ages))
        recent = (recent + [frozenset(alive)])[-3:]
    return history


def render(history, cols, theme_name, frame_seconds):
    theme = THEMES[theme_name]
    generations = len(history)
    width = PAD * 2 + cols * PITCH - (PITCH - CELL)
    height = PAD * 2 + ROWS * PITCH - (PITCH - CELL)

    def color(age):
        return theme["empty"] if not age else theme["ages"][age - 1]

    timelines, classes, rects = {}, [], []
    for row in range(ROWS):
        for col in range(cols):
            frames = tuple(color(h.get((row, col), 0)) for h in history)
            x = PAD + col * PITCH
            y = PAD + row * PITCH
            attrs = 'x="%d" y="%d" width="%d" height="%d" rx="2"' % (x, y, CELL, CELL)
            if len(set(frames)) == 1:
                rects.append('<rect %s fill="%s"/>' % (attrs, frames[0]))
                continue
            idx = timelines.setdefault(frames, len(timelines))
            if idx == len(classes):
                stops = ["0%%{fill:%s}" % frames[0]]
                for gen in range(1, generations):
                    if frames[gen] != frames[gen - 1]:
                        stops.append("%.3f%%{fill:%s}" % (gen * 100.0 / generations, frames[gen]))
                classes.append("@keyframes g%d{%s}" % (idx, "".join(stops)))
            # The presentation attribute is a fallback: CSS animation overrides it,
            # but without it a viewer that ignores the animation renders black.
            rects.append('<rect %s fill="%s" class="g%d"/>' % (attrs, frames[0], idx))

    css = [
        "rect{shape-rendering:geometricPrecision}",
        "[class^=g]{animation-duration:%.2fs;animation-iteration-count:infinite;"
        "animation-timing-function:step-end}" % (generations * frame_seconds),
    ]
    css += ['.g%d{animation-name:g%d}' % (i, i) for i in range(len(classes))]
    css += classes

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img" aria-label="Conway\'s Game of Life seeded '
        'by a GitHub contribution graph">\n<style>%s</style>\n%s\n</svg>\n'
        % (width, height, width, height, "".join(css), "".join(rects))
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--generations", type=int, default=110)
    ap.add_argument("--frame-seconds", type=float, default=0.32)
    ap.add_argument("--min-cells", type=int, default=90,
                    help="inject seed patterns until the board has at least this many live cells")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out-light", default="dist/game-of-life.svg")
    ap.add_argument("--out-dark", default="dist/game-of-life-dark.svg")
    args = ap.parse_args()

    grid, cols = load_grid(args.user, args.token)
    rng = random.Random(args.seed if args.seed is not None else abs(hash(args.user)) % (2 ** 31))
    alive, seeded = build_seed(grid, cols, args.min_cells, rng)
    print("grid %dx%d | %d cells from contributions | %d after seeding"
          % (cols, ROWS, seeded, len(alive)), file=sys.stderr)

    history = simulate(alive, cols, args.generations, rng, args.min_cells)
    import os
    for path, theme in ((args.out_light, "light"), (args.out_dark, "dark")):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            fh.write(render(history, cols, theme, args.frame_seconds))
        print("wrote %s (%d bytes)" % (path, os.path.getsize(path)), file=sys.stderr)


if __name__ == "__main__":
    main()
