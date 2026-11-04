"""Hand-counted fixtures derived from the frozen public facts metadata.

These helpers never call a production tally, renderer, percentage or exporter.
Every number is the sum of the counts of the lane rows it stands for in the
frozen fixture, so the aggregate is the old public numbers regrouped.
"""
import copy
import json
from pathlib import Path


FROZEN = Path(__file__).resolve().parent / "fixtures/repo-descriptions-facts.json"


def tiers(excellent=0, strong=0, moderate=0, weak=0, minimal=0, no_trades=0):
    return {"excellent": excellent, "strong": strong, "moderate": moderate,
            "weak": weak, "minimal": minimal, "no-trades": no_trades}


def aggregate_facts():
    facts = json.loads(FROZEN.read_text())
    first = dict(symbol="BINANCE:BTCUSDT", timeframe="15", hardProbes=1,
                 corpusProbes=2, closedProbes=2, graded=3, excellent=1,
                 strong=1, belowStrong=1, engineErrors=1, tiers=tiers(1, 1, 1))
    second = dict(symbol="BINANCE:BTCUSDT", timeframe="1D", hardProbes=0,
                  corpusProbes=1, closedProbes=2, graded=3, excellent=1,
                  strong=1, belowStrong=1, engineErrors=0, tiers=tiers(1, 1, weak=1))
    other = dict(lanes=3, markets=2, additionalTimeframes=1, hardProbes=2,
                 corpusProbes=1, closedProbes=4, graded=4, excellent=1,
                 strong=1, belowStrong=2, engineErrors=1,
                 tiers=tiers(1, 1, minimal=1, no_trades=1))
    numbers = dict(
        population=13, anomaliesExcluded=1, graded=10, excellent=3, strong=3,
        belowStrong=4, engineErrors=2, tiers=tiers(3, 3, 1, 1, 1, 1),
        excellentPct=30, strongPct=30, excellentOrStrongPct=60,
        lanes=5, markets=3, timeframes=3, corpusProbes=4, closedProbes=8,
        pairs=[first, second], otherMarkets=other, hardLane=copy.deepcopy(first),
        scopes={
            "corpus": dict(graded=3, excellent=1, strong=1, belowStrong=1,
                           engineErrors=1, tiers=tiers(1, 1, 1)),
            "closed": dict(graded=7, excellent=2, strong=2, belowStrong=3,
                           engineErrors=1, tiers=tiers(2, 2, weak=1, minimal=1, no_trades=1)),
        },
    )
    active = facts["scoreboard"]
    active.update(copy.deepcopy(numbers))
    historic = facts["releases"]["1.0.1"]
    historic["scoreboard"].update(copy.deepcopy(numbers))
    facts["releases"] = {"1.0.1": historic}
    facts["inventory"].update(corpusScripts=4, communityScripts=5, probeScripts=2,
                              closedScripts=7, closedProbes=8,
                              communityProbes=5, probeScriptProbes=3)
    return facts


def legacy_facts():
    """The same totals represented entirely by named crypto pairs."""
    facts = aggregate_facts()
    extra = [
        dict(symbol="BINANCE:SOLUSDT", timeframe="15", hardProbes=2,
             corpusProbes=1, closedProbes=1, graded=1, excellent=1,
             strong=0, belowStrong=0, engineErrors=1, tiers=tiers(1)),
        dict(symbol="BINANCE:SOLUSDT", timeframe="1D", hardProbes=0,
             corpusProbes=0, closedProbes=1, graded=1, excellent=0,
             strong=1, belowStrong=0, engineErrors=0, tiers=tiers(strong=1)),
        dict(symbol="BINANCE:ADAUSDT", timeframe="60", hardProbes=0,
             corpusProbes=0, closedProbes=2, graded=2, excellent=0,
             strong=0, belowStrong=2, engineErrors=0, tiers=tiers(minimal=1, no_trades=1)),
    ]
    for score in (facts["scoreboard"], facts["releases"]["1.0.1"]["scoreboard"]):
        score.pop("otherMarkets")
        score["pairs"].extend(copy.deepcopy(extra))
    return facts


def zero_aggregate():
    return dict(lanes=0, markets=0, additionalTimeframes=0, hardProbes=0,
                corpusProbes=0, closedProbes=0, graded=0, excellent=0,
                strong=0, belowStrong=0, engineErrors=0, tiers=tiers())


def description_facts():
    """All graded probes excellent or strong, with the same probe dimensions."""
    facts = aggregate_facts()
    for score in (facts["scoreboard"], facts["releases"]["1.0.1"]["scoreboard"]):
        score.update(excellent=6, strong=4, belowStrong=0, tiers=tiers(6, 4),
                     excellentPct=60, strongPct=40, excellentOrStrongPct=100)
        for pair in score["pairs"]:
            pair.update(excellent=2, strong=1, belowStrong=0, tiers=tiers(2, 1))
        score["hardLane"] = copy.deepcopy(score["pairs"][0])
        score["otherMarkets"].update(excellent=2, strong=2, belowStrong=0, tiers=tiers(2, 2))
        score["scopes"]["corpus"].update(excellent=2, strong=1, belowStrong=0, tiers=tiers(2, 1))
        score["scopes"]["closed"].update(excellent=4, strong=3, belowStrong=0, tiers=tiers(4, 3))
    return facts


def other_market_timeframe_rows():
    """Three hand-counted frames; string order is 15, 1D, 60."""
    return [
        dict(timeframe="15", lanes=1, graded=1, excellent=1, strong=0,
             belowStrong=0, engineErrors=1, tiers=tiers(excellent=1)),
        dict(timeframe="1D", lanes=1, graded=1, excellent=0, strong=1,
             belowStrong=0, engineErrors=0, tiers=tiers(strong=1)),
        dict(timeframe="60", lanes=1, graded=2, excellent=0, strong=0,
             belowStrong=2, engineErrors=0, tiers=tiers(minimal=1, no_trades=1)),
    ]


def shared_other_market_timeframe_rows():
    """The same four grades and three lanes, with two lanes sharing 15."""
    return [
        dict(timeframe="15", lanes=2, graded=2, excellent=1, strong=1,
             belowStrong=0, engineErrors=1, tiers=tiers(excellent=1, strong=1)),
        dict(timeframe="60", lanes=1, graded=2, excellent=0, strong=0,
             belowStrong=2, engineErrors=0, tiers=tiers(minimal=1, no_trades=1)),
    ]


def timeframe_facts(row_factory=other_market_timeframe_rows, targets=(0, 1)):
    """Add independently owned rows without changing any parent totals."""
    facts = aggregate_facts()
    scores = (facts["scoreboard"], facts["releases"]["1.0.1"]["scoreboard"])
    rows = row_factory()
    for target in targets:
        scores[target]["otherMarkets"]["timeframes"] = copy.deepcopy(rows)
    return facts


def all_tier_timeframe_facts(targets=(0, 1)):
    """Regroup two named below-strong grades into the anonymous aggregate.

    The ten overall grades stay 3 excellent, 3 strong and 1 in each lower
    tier. Each named pair now contributes 1 excellent and 1 strong; the
    anonymous six grades contribute exactly 1 in each of the six tiers.
    """
    facts = aggregate_facts()
    rows = [
        dict(timeframe="15", lanes=1, graded=2, excellent=1, strong=0,
             belowStrong=1, engineErrors=1, tiers=tiers(excellent=1, moderate=1)),
        dict(timeframe="1D", lanes=1, graded=2, excellent=0, strong=1,
             belowStrong=1, engineErrors=0, tiers=tiers(strong=1, weak=1)),
        dict(timeframe="60", lanes=1, graded=2, excellent=0, strong=0,
             belowStrong=2, engineErrors=0, tiers=tiers(minimal=1, no_trades=1)),
    ]
    scores = (facts["scoreboard"], facts["releases"]["1.0.1"]["scoreboard"])
    for target, score in enumerate(scores):
        for pair in score["pairs"]:
            pair.update(graded=2, belowStrong=0, closedProbes=1, tiers=tiers(1, 1))
        score["hardLane"] = copy.deepcopy(score["pairs"][0])
        score["otherMarkets"].update(
            graded=6, belowStrong=4, closedProbes=6, tiers=tiers(1, 1, 1, 1, 1, 1))
        if target in targets:
            score["otherMarkets"]["timeframes"] = copy.deepcopy(rows)
    return facts
