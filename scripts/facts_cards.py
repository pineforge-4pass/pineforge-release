#!/usr/bin/env python3
"""Pure deterministic SVG and endpoint JSON; no network or publishing."""
from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path

from facts_validate import InputError, load_facts


def render(facts, sha256, variant, format, theme):
    scoreboard = facts["scoreboard"]
    score = scoreboard if variant == "full" else scoreboard["scopes"]["corpus"]
    label = "PineForge scoreboard" if variant == "full" else "PineForge corpus scoreboard"
    message = (f'{score["excellent"]:,}/{score["graded"]:,} excellent; '
               f'{score["strong"]:,} strong; {score["belowStrong"]:,} below strong; '
               f'{score["engineErrors"]:,} engine errors')
    provenance = f'{scoreboard["date"]} · engine {scoreboard["engineCommit"][:8]}'
    if format == "json":
        return json.dumps({"schemaVersion": 1, "label": label, "message": message,
                           "color": "blue", "factsSha256": sha256,
                           "date": scoreboard["date"], "engineCommit": scoreboard["engineCommit"]},
                          ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    background, foreground, muted = (("#101820", "#f5f6f7", "#b7c4cb") if theme == "dark"
                                    else ("#f7f8f9", "#172731", "#405563"))
    description = f"{label}: {message}. {provenance}. Canonical facts: https://pineforge.dev/facts.json."
    # No timestamps, external assets, scripts, fonts, or floating-point layout.
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="880" height="240" viewBox="0 0 880 240" '
        'role="img" aria-labelledby="title desc">\n'
        f'  <title id="title">{escape(label)}</title>\n'
        f'  <desc id="desc">{escape(description)}</desc>\n'
        f'  <metadata>sha256:{sha256}</metadata>\n'
        f'  <rect width="880" height="240" rx="12" fill="{background}"/>\n'
        f'  <text x="28" y="40" fill="{foreground}" font-family="sans-serif" font-size="22">{escape(label)}</text>\n'
        f'  <text x="28" y="100" fill="{foreground}" font-family="sans-serif" font-size="38">'
        f'{score["excellent"]:,} / {score["graded"]:,} excellent probes</text>\n'
        f'  <text x="28" y="142" fill="{foreground}" font-family="sans-serif" font-size="19">'
        f'{score["strong"]:,} strong · {score["belowStrong"]:,} below strong · {score["engineErrors"]:,} engine errors</text>\n'
        f'  <text x="28" y="185" fill="{muted}" font-family="sans-serif" font-size="17">{escape(provenance)}</text>\n'
        f'  <text x="28" y="217" fill="{muted}" font-family="sans-serif" font-size="14">pineforge.dev/facts.json</text>\n'
        '</svg>\n'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--facts", required=True, type=Path)
    parser.add_argument("--variant", required=True, choices=("full", "corpus"))
    parser.add_argument("--format", choices=("svg", "json"), default="svg")
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    args = parser.parse_args()
    try:
        facts, sha = load_facts(args.facts)
        output = render(facts, sha, args.variant, args.format, args.theme)
        sys.stdout.write(output)
        return 0
    except (InputError, OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError):
        print("facts-cards: invalid facts or unavailable input", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
