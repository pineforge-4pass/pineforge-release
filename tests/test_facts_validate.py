"""Fixture CLI acceptance and exact schema/semantic refusal diagnostics."""
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "tests/fixtures/repo-descriptions-facts.json"
sys.path.insert(0, str(ROOT / "scripts"))

from facts_validate import InputError, validate_facts
from facts_other_markets import aggregate_facts, legacy_facts, tiers, zero_aggregate
from facts_other_markets import (
    all_tier_timeframe_facts, other_market_timeframe_rows,
    shared_other_market_timeframe_rows, timeframe_facts,
)


class FactsValidationCLI(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        facts = json.loads(FACTS.read_text())
        facts["scoreboard"].update(
            tvTradesCompared=1,
            tvTradesComparedSource=(
                "Sum over the graded probes of this baseline of the trades compared with TradingView, "
                "each probe counted once; a position closed in several parts counts once; "
                "trades still open at the end of TradingView's list are excluded."))
        self.facts = Path(temporary.name) / "facts.json"
        self.facts.write_text(json.dumps(facts))

    def invoke(self, path):
        return subprocess.run([sys.executable, str(ROOT / "scripts/facts_validate.py"),
                               "--facts", str(path)], capture_output=True, text=True)

    def test_current_and_historical_facts_validate(self):
        result = self.invoke(self.facts)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["sha256"], hashlib.sha256(self.facts.read_bytes()).hexdigest())

    def test_compared_trade_count_is_one_paired_active_integer_and_optional(self):
        canonical = json.loads(self.facts.read_text())
        score = canonical["scoreboard"]
        self.assertIsInstance(score["tvTradesCompared"], int)
        self.assertNotIsInstance(score["tvTradesCompared"], bool)
        self.assertGreater(score["tvTradesCompared"], 0)
        self.assertNotIn("tvTradesCompared", canonical["inventory"])
        for release in canonical["releases"].values():
            self.assertNotIn("tvTradesCompared", release["scoreboard"])
        # Both fields absent is a valid file: the pair is optional, never half present.
        absent = copy.deepcopy(canonical)
        absent["scoreboard"].pop("tvTradesCompared")
        absent["scoreboard"].pop("tvTradesComparedSource")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facts.json"
            path.write_text(json.dumps(absent))
            result = self.invoke(path)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_complete_schema_and_semantic_negatives_have_no_output(self):
        canonical = json.loads(self.facts.read_text())
        changes = {
            "root-extra": lambda f: f.update(extra=True),
            "missing": lambda f: f["scoreboard"].pop("pairs"),
            "bool-count": lambda f: f["scoreboard"].update(graded=True),
            "float-count": lambda f: f["scoreboard"].update(graded=7989.0),
            "unsafe-integer": lambda f: f["inventory"].update(tvTrades=2**53),
            "date": lambda f: f["scoreboard"].update(date="2026-02-30"),
            "timestamp": lambda f: f["scoreboard"]["provenance"].update(promotionDate="2026-10-06"),
            "invalid-timestamp-day": lambda f: f["scoreboard"]["provenance"].update(promotionDate="2026-02-30T12:00:00Z"),
            "non-utc-timestamp": lambda f: f["scoreboard"]["provenance"].update(promotionDate="2026-10-06T12:00:00+01:00"),
            "digest": lambda f: f["scoreboard"].update(snapshotSha256="a" * 64 + "\n"),
            "provenance": lambda f: f["scoreboard"]["provenance"].update(engineCommit="a" * 40),
            "source": lambda f: f["scoreboard"].update(source="unverified source"),
            "population": lambda f: f["scoreboard"].update(population=1),
            "pairs-sum": lambda f: f["scoreboard"]["pairs"][1].update(strong=7),
            "duplicate-pair": lambda f: f["scoreboard"]["pairs"].append(copy.deepcopy(f["scoreboard"]["pairs"][0])),
            "tiers": lambda f: f["scoreboard"]["tiers"].update(moderate=1),
            "scope": lambda f: f["scoreboard"]["scopes"]["corpus"].update(excellent=1),
            "hard-lane": lambda f: f["scoreboard"]["hardLane"].update(hardProbes=1),
            "lanes": lambda f: f["scoreboard"].update(lanes=1),
            "percentage": lambda f: f["scoreboard"].update(excellentPct=50),
            "symbol": lambda f: f["scoreboard"]["pairs"][0].update(symbol="bad/path"),
            "timeframe": lambda f: f["scoreboard"]["pairs"][0].update(timeframe="0"),
            "release-nested": lambda f: f["releases"]["1.0.1"]["scoreboard"]["pairs"][0].update(unknown=1),
            "release-provenance": lambda f: f["releases"]["1.0.1"]["scoreboard"].update(id="wrong"),
            "release-version": lambda f: f["releases"].update({"v1.0.0": f["releases"]["1.0.1"]}),
            "release-missing-source": lambda f: f["releases"]["1.0.1"].pop("source"),
            "release-identity": lambda f: f["releases"]["1.0.1"].update(source="https://github.com/pineforge-4pass/other/blob/v1.0.1/README.md"),
            "inventory-sum": lambda f: f["inventory"].update(closedScripts=1),
            "inventory-release": lambda f: f["inventory"].update(sourceRelease="99.0.0"),
            "inventory-digest": lambda f: f["inventory"].update(populationSha256="a" * 64),
            "inventory-source": lambda f: f["inventory"].update(source=f["inventory"]["source"] + " additional locator"),
            "inventory-missing": lambda f: f.pop("inventory"),
            "compared-without-source": lambda f: f["scoreboard"].pop("tvTradesComparedSource"),
            "source-without-compared": lambda f: f["scoreboard"].pop("tvTradesCompared"),
            "compared-source-text": lambda f: f["scoreboard"].update(
                tvTradesComparedSource=f["scoreboard"]["tvTradesComparedSource"] + " Exported by a tool."),
            "compared-negative": lambda f: f["scoreboard"].update(tvTradesCompared=-1),
            "compared-float": lambda f: f["scoreboard"].update(tvTradesCompared=1.5),
            "compared-bool": lambda f: f["scoreboard"].update(tvTradesCompared=True),
            "compared-unsafe-integer": lambda f: f["scoreboard"].update(tvTradesCompared=2**53),
            "compared-in-release": lambda f: f["releases"]["1.0.1"]["scoreboard"].update(
                tvTradesCompared=f["scoreboard"]["tvTradesCompared"],
                tvTradesComparedSource=f["scoreboard"]["tvTradesComparedSource"]),
            "compared-per-lane": lambda f: f["scoreboard"]["pairs"][0].update(tvTradesCompared=1),
            "compared-per-scope": lambda f: f["scoreboard"]["scopes"]["corpus"].update(tvTradesCompared=1),
            "trade-rows-public": lambda f: f["scoreboard"].update(tvTradeRows=1),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facts.json"
            for name, change in changes.items():
                with self.subTest(name=name):
                    value = copy.deepcopy(canonical)
                    change(value)
                    path.write_text(json.dumps(value))
                    result = self.invoke(path)
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertEqual(result.stdout, "")
                    self.assertTrue(result.stderr)


class OtherMarketsValidation(unittest.TestCase):
    def assert_invalid(self, facts, message):
        with self.assertRaisesRegex(InputError, "^" + re.escape(message) + "$"):
            validate_facts(facts)

    def scores(self, facts):
        return (facts["scoreboard"], facts["releases"]["1.0.1"]["scoreboard"])

    def test_active_and_release_accept_aggregate_and_identical_legacy_totals(self):
        for factory in (aggregate_facts, legacy_facts):
            with self.subTest(shape=factory.__name__):
                facts = factory()
                self.assertIs(validate_facts(facts), facts)
                for score in self.scores(facts):
                    self.assertEqual(score["graded"], 10)
                    self.assertEqual(score["lanes"], 5)
                    self.assertEqual(score["markets"], 3)
                    self.assertEqual(score["timeframes"], 3)

    def test_active_and_release_can_independently_omit_the_aggregate(self):
        for target in (0, 1):
            facts, legacy = aggregate_facts(), legacy_facts()
            score = self.scores(facts)[target]
            score.clear()
            score.update(copy.deepcopy(self.scores(legacy)[target]))
            with self.subTest(target=target):
                self.assertIs(validate_facts(facts), facts)

    def test_empty_aggregate_is_optional_and_valid(self):
        facts = legacy_facts()
        for score in self.scores(facts):
            score["otherMarkets"] = zero_aggregate()
        self.assertIs(validate_facts(facts), facts)

    def test_aggregate_bounds_accept_equality_and_nonzero_engine_errors(self):
        facts = aggregate_facts()
        for score in self.scores(facts):
            score.update(lanes=7, markets=6, timeframes=7)
            score["otherMarkets"].update(lanes=5, markets=5, additionalTimeframes=5, hardProbes=5)
        self.assertIs(validate_facts(facts), facts)

    def test_every_aggregate_tier_contributes_and_timeframes_can_outnumber_markets(self):
        facts = aggregate_facts()
        for score in self.scores(facts):
            for pair in score["pairs"]:
                pair.update(graded=2, belowStrong=0, closedProbes=1, tiers=tiers(1, 1))
            score["hardLane"] = copy.deepcopy(score["pairs"][0])
            score["otherMarkets"].update(graded=6, belowStrong=4, closedProbes=6,
                                          markets=1, additionalTimeframes=3,
                                          tiers=tiers(1, 1, 1, 1, 1, 1))
            score.update(markets=2, timeframes=5)
        self.assertIs(validate_facts(facts), facts)

    def test_hard_lane_names_only_the_first_pair_even_with_hard_aggregate(self):
        facts = aggregate_facts()
        for score in self.scores(facts):
            score["pairs"][0]["hardProbes"] = 0
            score["hardLane"] = None
        self.assertIs(validate_facts(facts), facts)
        for score in self.scores(facts):
            score["pairs"][1]["hardProbes"] = 1
            score["hardLane"] = copy.deepcopy(score["pairs"][1])
        self.assertIs(validate_facts(facts), facts)

    def test_no_named_pairs_can_still_have_counts_and_hard_probes(self):
        facts = aggregate_facts()
        for score in self.scores(facts):
            score["pairs"] = []
            score["hardLane"] = None
            score["otherMarkets"] = dict(
                lanes=5, markets=3, additionalTimeframes=3, hardProbes=3,
                corpusProbes=4, closedProbes=8, graded=10, excellent=3,
                strong=3, belowStrong=4, engineErrors=2, tiers=tiers(3, 3, 1, 1, 1, 1))
        self.assertIs(validate_facts(facts), facts)

    def test_aggregate_has_exact_required_fields_and_no_identity_or_nested_payload(self):
        changes = [
            ("symbol", lambda o: o.update(symbol="PRIVATE:INDEX"), "additionalProperties"),
            ("venue", lambda o: o.update(venue="PRIVATE"), "additionalProperties"),
            ("name", lambda o: o.update(name="private market"), "additionalProperties"),
            ("nested-extra", lambda o: o.update(identity={"symbol": "PRIVATE:INDEX"}), "additionalProperties"),
            ("array-extra", lambda o: o.update(identities=["PRIVATE:INDEX"]), "additionalProperties"),
            ("tier-key", lambda o: o["tiers"].update({"PRIVATE:INDEX": 0}), "additionalProperties"),
            ("tier-string", lambda o: o["tiers"].update(excellent="PRIVATE:INDEX"), "type"),
            ("tiers-array", lambda o: o.update(tiers=[]), "type"),
        ]
        for name, value in (("string", "PRIVATE:INDEX"), ("object", {"symbol": "PRIVATE:INDEX"}),
                            ("array", ["PRIVATE:INDEX"]), ("bool", True), ("float", 1.5)):
            changes.append(("nested-tier-" + name, lambda o, v=value: o["tiers"].update(weak=v), "type"))
        fields = ("lanes", "markets", "additionalTimeframes", "hardProbes", "corpusProbes",
                  "closedProbes", "graded", "excellent", "strong", "belowStrong", "engineErrors")
        for field in fields:
            changes.append(("missing-" + field, lambda o, f=field: o.pop(f), "required"))
            for name, value in (("string", "PRIVATE:INDEX"), ("object", {"symbol": "PRIVATE:INDEX"}),
                                ("array", ["PRIVATE:INDEX"]), ("bool", True), ("float", 1.5),
                                ("null", None)):
                changes.append((name + "-" + field, lambda o, f=field, v=value: o.update({f: v}), "type"))
            changes.append(("negative-" + field, lambda o, f=field: o.update({f: -1}), "minimum"))
        changes.append(("missing-tiers", lambda o: o.pop("tiers"), "required"))
        for name, change, validator in changes:
            for target in (0, 1):
                with self.subTest(case=name, target=target):
                    facts = aggregate_facts()
                    change(self.scores(facts)[target]["otherMarkets"])
                    self.assert_invalid(facts, f"facts schema validation failed ({validator})")
        for value in ("PRIVATE:INDEX", ["PRIVATE:INDEX"], None, True, 1):
            for target in (0, 1):
                with self.subTest(aggregate_type=type(value).__name__, target=target):
                    facts = aggregate_facts()
                    self.scores(facts)[target]["otherMarkets"] = value
                    self.assert_invalid(facts, "facts schema validation failed (type)")

    def test_aggregate_scalar_safety_and_percentage_check_remain_active(self):
        for target in (0, 1):
            for field in ("lanes", "graded", "hardProbes"):
                facts = aggregate_facts()
                self.scores(facts)[target]["otherMarkets"][field] = 2**53
                with self.subTest(target=target, field=field):
                    self.assert_invalid(facts, "unsafe facts number")
            facts = aggregate_facts()
            self.scores(facts)[target]["otherMarkets"]["hardProbes"] = 1.0
            self.assert_invalid(facts, "counts must be JSON integers")
            for field, wrong in (("excellentPct", 31), ("strongPct", 31), ("excellentOrStrongPct", 61)):
                facts = aggregate_facts()
                self.scores(facts)[target][field] = wrong
                with self.subTest(target=target, field=field):
                    self.assert_invalid(facts, "score percentage differs")

    def test_each_dimension_and_aggregate_bound(self):
        # Dimension totals are adjusted along with the aggregate, so its bounds
        # cannot accidentally be covered by the earlier dimension check.
        rows = [
            ("lanes", {"lanes": 6}, {}, "lane dimensions differ"),
            ("markets", {"markets": 4}, {}, "lane dimensions differ"),
            ("timeframes", {"timeframes": 4}, {}, "lane dimensions differ"),
            ("markets-bound", {"markets": 5}, {"markets": 4}, "other markets exceed lanes"),
            ("lanes-bound", {"lanes": 8}, {"lanes": 6}, "other lanes exceed probes"),
            ("timeframes-bound", {"timeframes": 6}, {"additionalTimeframes": 4},
             "other timeframes exceed lanes"),
            ("zero-markets", {"markets": 1}, {"markets": 0}, "other markets and lanes disagree on emptiness"),
            ("zero-lanes-nonzero-markets", {"lanes": 2, "timeframes": 2},
             {"lanes": 0, "additionalTimeframes": 0}, "other markets exceed lanes"),
        ]
        for name, totals, other, message in rows:
            for target in (0, 1):
                facts = aggregate_facts()
                score = self.scores(facts)[target]
                score.update(totals)
                score["otherMarkets"].update(other)
                with self.subTest(case=name, target=target):
                    self.assert_invalid(facts, message)

    def test_duplicate_lane_and_timeframe_rules_still_apply_to_named_pairs(self):
        for target in (0, 1):
            facts = aggregate_facts()
            score = self.scores(facts)[target]
            score["pairs"][1]["timeframe"] = "15"
            score["timeframes"] = 2
            self.assert_invalid(facts, "duplicate lane identity")
            facts = aggregate_facts()
            self.scores(facts)[target]["pairs"][1]["timeframe"] = "0"
            self.assert_invalid(facts, "invalid lane timeframe")

    def test_named_lane_probe_guards_and_first_hard_pair_order_are_preserved(self):
        for factory in (aggregate_facts, legacy_facts):
            for target in (0, 1):
                for field, value, message in (("closedProbes", 3, "lane scope sum differs"),
                                               ("hardProbes", 5, "hard probes exceed lane")):
                    facts = factory()
                    score = self.scores(facts)[target]
                    score["pairs"][0][field] = value
                    score["hardLane"] = copy.deepcopy(score["pairs"][0])
                    with self.subTest(shape=factory.__name__, target=target, field=field):
                        self.assert_invalid(facts, message)
                facts = factory()
                score = self.scores(facts)[target]
                score["pairs"][1]["hardProbes"] = 1
                score["hardLane"] = copy.deepcopy(score["pairs"][1])
                self.assert_invalid(facts, "hard lane differs from lane evidence")

    def test_aggregate_tally_and_probe_equations(self):
        rows = [
            ("all-graded", lambda o: o.update(belowStrong=3), "all-graded tier sum differs"),
            ("tiers", lambda o: o["tiers"].update(moderate=1), "tier sum differs from graded"),
            ("excellent-summary", lambda o: o["tiers"].update(excellent=0, strong=2), "tier summaries differ"),
            ("strong-summary", lambda o: o["tiers"].update(strong=0, minimal=2), "tier summaries differ"),
            ("below-summary", lambda o: o["tiers"].update(minimal=0, excellent=2), "tier summaries differ"),
            ("probe-balance", lambda o: o.update(closedProbes=5), "other markets scope sum differs"),
            ("hard-bound", lambda o: o.update(hardProbes=6), "hard probes exceed other markets"),
        ]
        for name, change, message in rows:
            for target in (0, 1):
                with self.subTest(case=name, target=target):
                    facts = aggregate_facts()
                    change(self.scores(facts)[target]["otherMarkets"])
                    self.assert_invalid(facts, message)

    def test_lane_counts_tiers_and_scope_probes_include_aggregate(self):
        def extra_graded(score):
            other = score["otherMarkets"]
            other.update(graded=5, excellent=2, closedProbes=5)
            other["tiers"]["excellent"] = 2

        def extra_errors(score):
            score["otherMarkets"].update(engineErrors=2, closedProbes=5)

        def exchange(score, first, second):
            other = score["otherMarkets"]
            other[first] += 1
            other[second] -= 1
            other["tiers"][first] += 1
            other["tiers"]["minimal" if second == "belowStrong" else second] -= 1

        def move_probe(score):
            score["otherMarkets"].update(corpusProbes=2, closedProbes=3)

        def move_tier(score, tier):
            # Keep each group's summaries and graded total intact.
            other = score["otherMarkets"]["tiers"]
            other[tier] += 1
            other["no-trades" if tier != "no-trades" else "minimal"] -= 1

        rows = [
            ("graded", extra_graded, "lane count sum differs"),
            ("excellent", lambda s: exchange(s, "excellent", "strong"), "lane count sum differs"),
            ("strong", lambda s: exchange(s, "strong", "belowStrong"), "lane count sum differs"),
            ("engineErrors", extra_errors, "lane count sum differs"),
            ("corpusProbes", move_probe, "scope probes differ"),
            ("corpusProbes-total", lambda s: s.update(corpusProbes=5), "scope probes differ"),
            ("closedProbes", lambda s: s.update(closedProbes=9), "scope probes differ"),
            ("hardLane", lambda s: s.update(hardLane=None), "hard lane differs from lane evidence"),
        ]
        for tier in ("moderate", "weak", "minimal", "no-trades"):
            rows.append(("tier-" + tier, lambda s, t=tier: move_tier(s, t), "lane tier sum differs"))
        for name, change, message in rows:
            for target in (0, 1):
                with self.subTest(case=name, target=target):
                    facts = aggregate_facts()
                    change(self.scores(facts)[target])
                    self.assert_invalid(facts, message)

    def test_scope_count_and_tier_sums_still_bind_complete_scoreboard(self):
        def change_count(score, field):
            score["scopes"]["closed"][field] += 1

        def change_tier(score, tier):
            closed = score["scopes"]["closed"]["tiers"]
            closed[tier] += 1
            closed["no-trades" if tier != "no-trades" else "minimal"] -= 1

        rows = [(field, lambda s, f=field: change_count(s, f), "scope count sum differs")
                for field in ("graded", "excellent", "strong", "belowStrong", "engineErrors")]
        rows.extend((tier, lambda s, t=tier: change_tier(s, t), "scope tier sum differs")
                    for tier in ("moderate", "weak", "minimal", "no-trades"))
        for name, change, message in rows:
            for target in (0, 1):
                with self.subTest(case=name, target=target):
                    facts = aggregate_facts()
                    change(self.scores(facts)[target])
                    self.assert_invalid(facts, message)


class CryptoPairValidation(unittest.TestCase):
    def scores(self, facts):
        return (facts["scoreboard"], facts["releases"]["1.0.1"]["scoreboard"])

    def assert_invalid(self, facts, message):
        with self.assertRaisesRegex(InputError, "^" + re.escape(message) + "$"):
            validate_facts(facts)

    def test_each_crypto_venue_accepts_pairs_and_hard_lane_with_optional_aggregate(self):
        venues = (
            "BINANCE", "BINANCEUS", "BYBIT", "COINBASE", "KRAKEN", "BITSTAMP",
            "BITFINEX", "OKX", "KUCOIN", "GATEIO", "MEXC", "HUOBI", "HTX", "BITGET",
        )
        for factory in (aggregate_facts, legacy_facts):
            for target in (0, 1):
                for venue in venues:
                    with self.subTest(shape=factory.__name__, target=target, venue=venue):
                        facts = factory()
                        score = self.scores(facts)[target]
                        for pair in score["pairs"][:2]:
                            pair["symbol"] = venue + ":BTCUSDT"
                        score["hardLane"] = copy.deepcopy(score["pairs"][0])
                        self.assertIs(validate_facts(facts), facts)
                        self.assertEqual(score["hardLane"], score["pairs"][0])
                        self.assertEqual("otherMarkets" in score, factory is aggregate_facts)

    def test_ticker_accepts_digits_underscore_period_and_exclamation(self):
        for target in (0, 1):
            with self.subTest(target=target):
                facts = aggregate_facts()
                score = self.scores(facts)[target]
                for pair in score["pairs"]:
                    pair["symbol"] = "KRAKEN:XBT_USD.P1!"
                score["hardLane"] = copy.deepcopy(score["pairs"][0])
                self.assertIs(validate_facts(facts), facts)

    def test_unlisted_venues_and_malformed_pair_symbols_fail_schema(self):
        symbols = (
            "NASDAQ:AAPL",
            "NYSE:IBM",
            "OANDA:EURUSD",
            "CME_MINI:ES1!",
            "NSE:NIFTY",
            "FOO:BTCUSDT",
            "binance:BTCUSDT",
            "BTCUSDT",
            "BINANCEX:BTCUSDT",
            "BINANCE:BTC-USDT",
            "BINANCE:BTCUSDT:PERP",
            "BINANCEBTCUSDT",
            "BINANCE:",
            "XBINANCE:BTCUSDT",
            "BINANCE:BTCUSDT?",
        )
        for factory in (aggregate_facts, legacy_facts):
            for target in (0, 1):
                for symbol in symbols:
                    with self.subTest(shape=factory.__name__, target=target, symbol=symbol):
                        facts = factory()
                        score = self.scores(facts)[target]
                        # Only the non-hard pair changes, adding one distinct named market.
                        score["pairs"][1]["symbol"] = symbol
                        score["markets"] += 1
                        self.assert_invalid(facts, "facts schema validation failed (pattern)")

    def test_non_crypto_hard_lane_fails_its_schema_branch(self):
        for factory in (aggregate_facts, legacy_facts):
            for target in (0, 1):
                with self.subTest(shape=factory.__name__, target=target):
                    facts = factory()
                    self.scores(facts)[target]["hardLane"]["symbol"] = "NASDAQ:AAPL"
                    self.assert_invalid(facts, "facts schema validation failed (anyOf)")

    def test_crypto_hard_lane_still_requires_first_hard_pair_evidence(self):
        for factory in (aggregate_facts, legacy_facts):
            for target in (0, 1):
                with self.subTest(shape=factory.__name__, target=target):
                    facts = factory()
                    score = self.scores(facts)[target]
                    self.assertEqual(score["hardLane"], score["pairs"][0])
                    self.assertIs(validate_facts(facts), facts)
                    score["hardLane"]["symbol"] = "COINBASE:BTCUSD"
                    self.assert_invalid(facts, "hard lane differs from lane evidence")


class OtherMarketsTimeframesValidation(unittest.TestCase):
    """Exercise the real schema and validator with independent input oracles."""

    SCALARS = ("lanes", "graded", "excellent", "strong", "belowStrong", "engineErrors")
    TIER_NAMES = ("excellent", "strong", "moderate", "weak", "minimal", "no-trades")
    TARGETS = ((0,), (1,), (0, 1))

    def scores(self, facts):
        return (facts["scoreboard"], facts["releases"]["1.0.1"]["scoreboard"])

    def assert_valid(self, facts):
        try:
            actual = validate_facts(facts)
        except InputError as error:
            self.fail(f"expected valid timeframe facts; validate_facts rejected them: {error}")
        self.assertIs(actual, facts)

    def assert_invalid(self, facts):
        with self.assertRaises(InputError):
            validate_facts(facts)

    def validated_case(self, target, factory=timeframe_facts):
        facts = factory(targets=(target,))
        # A negative only counts after this exact unmutated input validates.
        self.assert_valid(facts)
        return facts, self.scores(facts)[target]["otherMarkets"]["timeframes"]

    def assert_partition_totals(self, rows, counts=(3, 4, 1, 1, 2, 1),
                                tier_counts=(1, 1, 0, 0, 1, 1)):
        # Expected tuples are handwritten, never obtained from production.
        self.assertEqual(tuple(sum(row[field] for row in rows) for field in self.SCALARS),
                         counts)
        self.assertEqual(tuple(sum(row["tiers"][tier] for row in rows)
                               for tier in self.TIER_NAMES), tier_counts)

    def assert_row_tallies(self, rows):
        # Check isolation preconditions without invoking the production tally.
        for row in rows:
            self.assertEqual(row["excellent"] + row["strong"] + row["belowStrong"],
                             row["graded"])
            self.assertEqual(sum(row["tiers"].values()), row["graded"])
            self.assertEqual(
                (row["excellent"], row["strong"], row["belowStrong"]),
                (row["tiers"]["excellent"], row["tiers"]["strong"],
                 sum(row["tiers"][tier] for tier in self.TIER_NAMES[2:])))

    def test_legacy_documents_without_timeframe_arrays_remain_valid(self):
        for factory in (aggregate_facts, legacy_facts):
            with self.subTest(shape=factory.__name__):
                facts = factory()
                for score in self.scores(facts):
                    self.assertNotIn("timeframes", score.get("otherMarkets", {}))
                self.assert_valid(facts)

    def test_three_and_shared_frame_oracles_accept_active_archive_and_both(self):
        for row_factory in (other_market_timeframe_rows, shared_other_market_timeframe_rows):
            for targets in self.TARGETS:
                with self.subTest(rows=row_factory.__name__, targets=targets):
                    facts = timeframe_facts(row_factory, targets)
                    control = aggregate_facts()
                    self.assert_valid(control)
                    self.assert_valid(facts)
                    for target, score in enumerate(self.scores(facts)):
                        other = copy.deepcopy(score["otherMarkets"])
                        if target in targets:
                            rows = other.pop("timeframes")
                            self.assert_partition_totals(rows)
                            self.assert_row_tallies(rows)
                        else:
                            self.assertNotIn("timeframes", other)
                        self.assertEqual(other, self.scores(control)[target]["otherMarkets"])
                        self.assertEqual(score["timeframes"], 3)
                        self.assertEqual(other["additionalTimeframes"], 1)
                        unchanged = copy.deepcopy(score)
                        unchanged["otherMarkets"] = other
                        self.assertEqual(unchanged, self.scores(control)[target])

    def test_all_six_tiers_accept_active_archive_and_both(self):
        for targets in self.TARGETS:
            with self.subTest(targets=targets):
                facts = all_tier_timeframe_facts(targets)
                control = all_tier_timeframe_facts(targets=())
                self.assert_valid(control)
                self.assert_valid(facts)
                for target in targets:
                    score = self.scores(facts)[target]
                    self.assertEqual(score["graded"], 10)
                    self.assertEqual(score["tiers"], tiers(3, 3, 1, 1, 1, 1))
                    rows = score["otherMarkets"]["timeframes"]
                    self.assert_partition_totals(rows, (3, 6, 1, 1, 4, 1),
                                                 (1, 1, 1, 1, 1, 1))
                    self.assert_row_tallies(rows)

    def test_canonical_labels_use_string_order_without_renumbering_dimensions(self):
        # 10 precedes 2 in string order; every allowed suffix is exercised.
        labels = (("1", "2"), ("10", "2"), ("1D", "1M"), ("1S", "1W"),
                  ("15", "999999999999999999999999999999"))
        for target in (0, 1):
            for first, second in labels:
                with self.subTest(target=target, labels=(first, second)):
                    facts = timeframe_facts(shared_other_market_timeframe_rows, (target,))
                    rows = self.scores(facts)[target]["otherMarkets"]["timeframes"]
                    rows[0]["timeframe"], rows[1]["timeframe"] = first, second
                    self.assert_partition_totals(rows)
                    self.assert_valid(facts)
                    self.assertEqual(self.scores(facts)[target]["timeframes"], 3)
                    self.assertEqual(
                        self.scores(facts)[target]["otherMarkets"]["additionalTimeframes"], 1)

    def test_every_release_independently_accepts_or_omits_timeframes(self):
        selections = (
            (), ("active",), ("1.0.1",), ("1.0.2",),
            ("active", "1.0.1"), ("active", "1.0.2"), ("1.0.1", "1.0.2"),
            ("active", "1.0.1", "1.0.2"),
        )
        for selected in selections:
            with self.subTest(selected=selected):
                facts = aggregate_facts()
                later = copy.deepcopy(facts["releases"]["1.0.1"])
                later["source"] = (
                    "https://github.com/pineforge-4pass/pineforge-engine/"
                    "blob/v1.0.2/README.md#validation-scoreboard")
                facts["releases"]["1.0.2"] = later
                self.assert_valid(facts)
                scores = {"active": facts["scoreboard"],
                          "1.0.1": facts["releases"]["1.0.1"]["scoreboard"],
                          "1.0.2": later["scoreboard"]}
                for name in selected:
                    rows = (shared_other_market_timeframe_rows() if name == "1.0.2"
                            else other_market_timeframe_rows())
                    scores[name]["otherMarkets"]["timeframes"] = copy.deepcopy(rows)
                self.assert_valid(facts)

    def test_later_release_timeframes_are_always_validated(self):
        facts = aggregate_facts()
        later = copy.deepcopy(facts["releases"]["1.0.1"])
        later["source"] = (
            "https://github.com/pineforge-4pass/pineforge-engine/"
            "blob/v1.0.2/README.md#validation-scoreboard")
        later["scoreboard"]["otherMarkets"]["timeframes"] = other_market_timeframe_rows()
        facts["releases"]["1.0.2"] = later
        self.assert_valid(facts)
        later["scoreboard"]["otherMarkets"]["timeframes"][0]["engineErrors"] = 0
        self.assert_invalid(facts)

    def test_zero_aggregate_accepts_empty_arrays_independently(self):
        for targets in self.TARGETS:
            with self.subTest(targets=targets):
                facts = legacy_facts()
                for target, score in enumerate(self.scores(facts)):
                    score["otherMarkets"] = zero_aggregate()
                    if target in targets:
                        score["otherMarkets"]["timeframes"] = []
                self.assert_valid(facts)

    def test_nonzero_aggregate_rejects_empty_arrays(self):
        for target in (0, 1):
            with self.subTest(target=target):
                facts, rows = self.validated_case(target)
                rows.clear()
                self.assert_invalid(facts)

    def test_error_only_rows_accept_safe_maximum_and_reject_empty_arrays(self):
        # Synthetic boundary oracle: zero grades and exactly count error probes.
        # No graded count can mask a missing engineErrors sum check on empty [].
        for count in (1, 2**53 - 1):
            with self.subTest(engine_errors=count):
                facts = aggregate_facts()
                for score in self.scores(facts):
                    other = zero_aggregate()
                    other.update(lanes=1, markets=1, additionalTimeframes=1,
                                 closedProbes=count, engineErrors=count)
                    other["timeframes"] = [
                        dict(timeframe="1S", lanes=1, graded=0, excellent=0, strong=0,
                             belowStrong=0, engineErrors=count, tiers=tiers()),
                    ]
                    score.update(
                        population=count, anomaliesExcluded=0, graded=0, excellent=0,
                        strong=0, belowStrong=0, engineErrors=count, tiers=tiers(),
                        excellentPct=0, strongPct=0, excellentOrStrongPct=0,
                        lanes=1, markets=1, timeframes=1, corpusProbes=0,
                        closedProbes=count, pairs=[], hardLane=None, otherMarkets=other,
                        scopes={
                            "corpus": dict(graded=0, excellent=0, strong=0, belowStrong=0,
                                           engineErrors=0, tiers=tiers()),
                            "closed": dict(graded=0, excellent=0, strong=0, belowStrong=0,
                                           engineErrors=count, tiers=tiers()),
                        })
                facts["inventory"].update(corpusScripts=0, closedProbes=count,
                                          communityProbes=count, probeScriptProbes=0)
                self.assert_valid(facts)
                for target in (0, 1):
                    with self.subTest(target=target):
                        changed = copy.deepcopy(facts)
                        self.scores(changed)[target]["otherMarkets"]["timeframes"] = []
                        self.assert_invalid(changed)

    def test_duplicate_only_split_preserves_totals_but_is_rejected(self):
        for target in (0, 1):
            with self.subTest(target=target):
                facts = timeframe_facts(shared_other_market_timeframe_rows, (target,))
                self.assert_valid(facts)
                # Split shared 15 back into its two original hand-counted lanes.
                # Only the second label changes; no scalar or tier total changes.
                rows = other_market_timeframe_rows()
                rows[1]["timeframe"] = "15"
                self.assertEqual([row["timeframe"] for row in rows], ["15", "15", "60"])
                self.assert_partition_totals(rows)
                self.assert_row_tallies(rows)
                self.scores(facts)[target]["otherMarkets"]["timeframes"] = rows
                self.assert_invalid(facts)

    def test_order_only_reversal_preserves_totals_but_is_rejected(self):
        for row_factory in (other_market_timeframe_rows, shared_other_market_timeframe_rows):
            for target in (0, 1):
                with self.subTest(rows=row_factory.__name__, target=target):
                    facts = timeframe_facts(row_factory, (target,))
                    self.assert_valid(facts)
                    rows = self.scores(facts)[target]["otherMarkets"]["timeframes"]
                    rows.reverse()
                    self.assert_partition_totals(rows)
                    self.assert_row_tallies(rows)
                    self.assert_invalid(facts)

    def test_each_row_requires_exactly_eight_fields(self):
        fields = ("timeframe", *self.SCALARS, "tiers")
        for target in (0, 1):
            control, _ = self.validated_case(target)
            for field in fields:
                with self.subTest(target=target, missing=field):
                    facts = copy.deepcopy(control)
                    self.scores(facts)[target]["otherMarkets"]["timeframes"][0].pop(field)
                    self.assert_invalid(facts)
            for field in ("extra", "hardProbes", "corpusProbes", "closedProbes"):
                with self.subTest(target=target, extra=field):
                    facts = copy.deepcopy(control)
                    self.scores(facts)[target]["otherMarkets"]["timeframes"][0][field] = 0
                    self.assert_invalid(facts)

    def test_forbidden_identity_provenance_and_trade_payloads_are_rejected(self):
        forbidden = (
            "symbol", "market", "venue", "assetClass", "asset-class", "id", "slug",
            "provenance", "privateProvenance", "privateEvidenceSha256",
            "trades", "tradeBreakdown", "tvTradesCompared", "tvTradeRows",
        )
        payloads = ("WITHHELD", {"symbol": "WITHHELD"}, [{"assetClass": "WITHHELD"}])
        for target in (0, 1):
            control, _ = self.validated_case(target)
            for field in forbidden:
                for location in ("row", "tiers"):
                    for payload in payloads:
                        with self.subTest(target=target, field=field, location=location,
                                          payload=type(payload).__name__):
                            facts = copy.deepcopy(control)
                            row = self.scores(facts)[target]["otherMarkets"]["timeframes"][0]
                            destination = row if location == "row" else row["tiers"]
                            destination[field] = copy.deepcopy(payload)
                            self.assert_invalid(facts)

    def test_timeframes_and_rows_require_array_and_object_types(self):
        nonarrays = (None, {}, "15", 15, 1.5, True)
        bad_rows = (None, [], "15", 15, 1.5, True)
        for target in (0, 1):
            control, _ = self.validated_case(target)
            for value in nonarrays:
                with self.subTest(target=target, array_type=type(value).__name__):
                    facts = copy.deepcopy(control)
                    self.scores(facts)[target]["otherMarkets"]["timeframes"] = copy.deepcopy(value)
                    self.assert_invalid(facts)
            for value in bad_rows:
                with self.subTest(target=target, row_type=type(value).__name__):
                    facts = copy.deepcopy(control)
                    self.scores(facts)[target]["otherMarkets"]["timeframes"][0] = copy.deepcopy(value)
                    self.assert_invalid(facts)

    def test_invalid_blank_and_noncanonical_timeframe_labels_are_rejected(self):
        labels = (
            "", " ", "0", "00", "0D", "00S", "01", "01D", "01W", "1d", "1s", "1w", "1m",
            "D", "-1", "+1", "1.0", "1H", "1DD", "1 D", " 1", "1 ", "1\n", "1D\n",
            "\u0661", "\uff11", None, True, 15, 15.0, [], {"symbol": "WITHHELD"},
        )
        for target in (0, 1):
            control, _ = self.validated_case(target)
            for value in labels:
                with self.subTest(target=target, label=value):
                    facts = copy.deepcopy(control)
                    self.scores(facts)[target]["otherMarkets"]["timeframes"][0]["timeframe"] = (
                        copy.deepcopy(value))
                    self.assert_invalid(facts)

    def test_all_scalar_and_tier_counts_require_safe_json_integers(self):
        values = (True, False, 0.0, 0.5, 1.0, -1, 2**53, "1", None,
                  {"symbol": "WITHHELD"}, [{"market": "WITHHELD"}])
        for target in (0, 1):
            control, _ = self.validated_case(target)
            for location, fields in (("row", self.SCALARS), ("tiers", self.TIER_NAMES)):
                for field in fields:
                    for value in values:
                        with self.subTest(target=target, location=location, field=field,
                                          value=value):
                            facts = copy.deepcopy(control)
                            row = self.scores(facts)[target]["otherMarkets"]["timeframes"][0]
                            destination = row if location == "row" else row["tiers"]
                            destination[field] = copy.deepcopy(value)
                            self.assert_invalid(facts)

    def test_tiers_require_the_existing_exact_six_keys(self):
        for target in (0, 1):
            control, _ = self.validated_case(target)
            for tier in self.TIER_NAMES:
                with self.subTest(target=target, missing_tier=tier):
                    facts = copy.deepcopy(control)
                    self.scores(facts)[target]["otherMarkets"]["timeframes"][0]["tiers"].pop(tier)
                    self.assert_invalid(facts)
            for value in (None, [], "tiers", True, 0, {"excellent": 1},
                          dict(tiers(excellent=1), extra=0)):
                with self.subTest(target=target, tiers=value):
                    facts = copy.deepcopy(control)
                    self.scores(facts)[target]["otherMarkets"]["timeframes"][0]["tiers"] = (
                        copy.deepcopy(value))
                    self.assert_invalid(facts)

    def test_each_scalar_sum_must_match_its_other_markets_aggregate(self):
        # Dependent grade/summary fields move together so every row still tallies.
        cases = (
            ("lanes", 0, dict(lanes=2), (4, 4, 1, 1, 2, 1), (1, 1, 0, 0, 1, 1)),
            ("graded", 0, dict(graded=2, excellent=2, tiers=tiers(excellent=2)),
             (3, 5, 2, 1, 2, 1), (2, 1, 0, 0, 1, 1)),
            ("excellent", 0, dict(excellent=0, strong=1, tiers=tiers(strong=1)),
             (3, 4, 0, 2, 2, 1), (0, 2, 0, 0, 1, 1)),
            ("strong", 1, dict(strong=0, belowStrong=1, tiers=tiers(weak=1)),
             (3, 4, 1, 0, 3, 1), (1, 0, 0, 1, 1, 1)),
            ("belowStrong", 2, dict(excellent=1, belowStrong=1,
                                   tiers=tiers(excellent=1, no_trades=1)),
             (3, 4, 2, 1, 1, 1), (2, 1, 0, 0, 0, 1)),
            ("engineErrors", 0, dict(engineErrors=0),
             (3, 4, 1, 1, 2, 0), (1, 1, 0, 0, 1, 1)),
        )
        for target in (0, 1):
            for field, index, change, counts, tier_counts in cases:
                with self.subTest(target=target, field=field):
                    facts, rows = self.validated_case(target)
                    rows[index].update(copy.deepcopy(change))
                    self.assert_row_tallies(rows)
                    self.assert_partition_totals(rows, counts, tier_counts)
                    self.assert_invalid(facts)

    def test_each_tier_sum_must_match_its_other_markets_aggregate(self):
        # Lower-tier exchanges keep all six scalar sums correct; excellent and
        # strong necessarily move their corresponding scalar summaries as well.
        cases = (
            ("excellent", 0, dict(excellent=0, strong=1,
                                 tiers=tiers(strong=1, moderate=1)),
             (3, 6, 0, 2, 4, 1), (0, 2, 1, 1, 1, 1)),
            ("strong", 1, dict(excellent=1, strong=0, tiers=tiers(excellent=1, weak=1)),
             (3, 6, 2, 0, 4, 1), (2, 0, 1, 1, 1, 1)),
            ("moderate", 0, dict(tiers=tiers(excellent=1, weak=1)),
             (3, 6, 1, 1, 4, 1), (1, 1, 0, 2, 1, 1)),
            ("weak", 1, dict(tiers=tiers(strong=1, minimal=1)),
             (3, 6, 1, 1, 4, 1), (1, 1, 1, 0, 2, 1)),
            ("minimal", 2, dict(tiers=tiers(no_trades=2)),
             (3, 6, 1, 1, 4, 1), (1, 1, 1, 1, 0, 2)),
            ("no-trades", 2, dict(tiers=tiers(minimal=2)),
             (3, 6, 1, 1, 4, 1), (1, 1, 1, 1, 2, 0)),
        )
        for target in (0, 1):
            for tier, index, change, counts, tier_counts in cases:
                with self.subTest(target=target, tier=tier):
                    facts, rows = self.validated_case(target, all_tier_timeframe_facts)
                    rows[index].update(copy.deepcopy(change))
                    self.assert_row_tallies(rows)
                    self.assert_partition_totals(rows, counts, tier_counts)
                    self.assert_invalid(facts)

    def test_each_row_tallies_even_when_cross_row_totals_match(self):
        cases = (
            ("all-graded", {0: dict(graded=2), 2: dict(graded=1)}),
            ("tier-sum", {0: dict(tiers=tiers()),
                          2: dict(tiers=tiers(excellent=1, minimal=1, no_trades=1))}),
            ("excellent-summary", {0: dict(tiers=tiers(minimal=1)),
                                   2: dict(tiers=tiers(excellent=1, no_trades=1))}),
            ("strong-summary", {1: dict(tiers=tiers(no_trades=1)),
                                2: dict(tiers=tiers(strong=1, minimal=1))}),
            ("below-summary", {0: dict(tiers=tiers(no_trades=1)),
                               2: dict(tiers=tiers(excellent=1, minimal=1))}),
        )
        for target in (0, 1):
            for name, changes in cases:
                with self.subTest(target=target, equation=name):
                    facts, rows = self.validated_case(target)
                    for index, change in changes.items():
                        rows[index].update(copy.deepcopy(change))
                    self.assert_partition_totals(rows)
                    self.assert_invalid(facts)

    def test_changed_input_cannot_reuse_a_constant_or_cached_success(self):
        for factory in (timeframe_facts, all_tier_timeframe_facts):
            for target in (0, 1):
                with self.subTest(shape=factory.__name__, target=target):
                    facts = factory()
                    self.assert_valid(facts)
                    rows = self.scores(facts)[target]["otherMarkets"]["timeframes"]
                    untouched = self.scores(facts)[1 - target]["otherMarkets"]["timeframes"]
                    self.assertIsNot(rows, untouched)
                    self.assertIsNot(rows[0], untouched[0])
                    self.assertIsNot(rows[0]["tiers"], untouched[0]["tiers"])
                    saved_rows, saved_untouched = copy.deepcopy(rows), copy.deepcopy(untouched)
                    # Mutate the same document after successful validation.
                    rows[0]["lanes"] = 2
                    self.assert_invalid(facts)
                    self.assertEqual(untouched, saved_untouched)
                    rows[:] = copy.deepcopy(saved_rows)
                    self.assert_valid(facts)
                    rows[0]["tiers"]["weak"] = True
                    self.assert_invalid(facts)
                    rows[:] = copy.deepcopy(saved_rows)
                    self.assert_valid(facts)

    def test_valid_timeframes_preserve_existing_scoreboard_guards(self):
        def exceed_other_markets(score):
            # Keep the old dimension equation valid to isolate the bound.
            score["markets"] = 5
            score["otherMarkets"]["markets"] = 4

        cases = (
            ("provenance", lambda s: s["provenance"].update(engineCommit="a" * 40)),
            ("scope", lambda s: s["scopes"]["closed"].update(engineErrors=2)),
            ("aggregate-scope", lambda s: s["otherMarkets"].update(closedProbes=5)),
            ("aggregate-bound", exceed_other_markets),
            ("dimensions", lambda s: s.update(timeframes=4)),
            ("percentage", lambda s: s.update(excellentPct=31)),
            ("named-timeframe", lambda s: s["pairs"][1].update(timeframe="0")),
        )
        for target in (0, 1):
            for name, change in cases:
                with self.subTest(target=target, guard=name):
                    facts, _ = self.validated_case(target)
                    change(self.scores(facts)[target])
                    self.assert_invalid(facts)


if __name__ == "__main__":
    unittest.main()
