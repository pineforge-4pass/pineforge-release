#!/usr/bin/env python3
"""Judge receipt cases with ONE scripts directory's validator; print one canonical verdict per case.

Not a test. tests/test_release_application_f6_parity.py runs this once against the pre-change
checkout's scripts directory and once against this tree's, then compares the output bytes. A case
carries everything its verdict depends on (receipt, hub event, policy, reporting run, repository and
the provider's answers), so nothing here knows or checks what a correct answer is: it only records
what the validator in that directory did, including every provider route it asked for, in order.

usage: legacy_receipt_verdicts.py <scripts-directory> <cases.json>
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path


class Unserved(Exception):
    """The validator asked for a route the case's provider does not hold."""


class Provider:
    def __init__(self, routes):
        self.routes, self.asked = routes, []

    def api(self, route):
        self.asked.append(route)
        if route not in self.routes:
            raise Unserved(route)
        return copy.deepcopy(self.routes[route])


def judge(receipts, case):
    provider = Provider(case["routes"])
    try:
        public = receipts.validate_receipt(provider, case["repo"], case["receipt"], case["event"],
                                           case["policy"], case["run"])
        verdict = {"accepted": True, "public": public}
    except Unserved as error:
        verdict = {"accepted": False, "unserved": error.args[0]}
    except Exception as error:  # a crash is a verdict too, and must be the same before and after
        verdict = {"accepted": False, "error": type(error).__name__, "message": str(error)}
    return {"name": case["name"], "verdict": verdict, "asked": provider.asked}


def main(argv):
    scripts, cases_path = argv
    sys.path.insert(0, scripts)
    import release_receipts as receipts
    print(receipts.__file__, file=sys.stderr)
    cases = json.loads(Path(cases_path).read_bytes())
    for case in cases:
        sys.stdout.write(json.dumps(judge(receipts, case), sort_keys=True, separators=(",", ":"),
                                    allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
