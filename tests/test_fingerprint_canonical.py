#!/usr/bin/env python3
"""Focused RFC 8785 / JCS fingerprint regression for docker/run_json.py.

Loads only the fingerprint-helper block (between markers) so this runs
without a native engine / strategy.so. Exit 0 iff every check passes.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import importlib.util
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUN_JSON = REPO / "docker" / "run_json.py"


class _TaggedStr(str):
    """Str subclass: tag-based eq/hash lets duplicate normalized keys coexist."""

    def __new__(cls, text: str, tag: object):
        obj = str.__new__(cls, text)
        obj.tag = tag
        return obj

    def __eq__(self, other):
        if isinstance(other, _TaggedStr):
            return (self.tag == other.tag
                    and str.__str__(self) == str.__str__(other))
        return NotImplemented

    def __hash__(self):
        return hash((_TaggedStr, str.__str__(self), self.tag))

    def __str__(self) -> str:
        return "hostile"


def _load_helpers():
    """Exec only the marked helper block — no ctypes / native engine load."""
    text = RUN_JSON.read_text(encoding="utf-8")
    start = text.index("# >>> fingerprint helpers")
    end = text.index("# <<< fingerprint helpers") + len("# <<< fingerprint helpers")
    block = text[start:end]
    mod = types.ModuleType("pf_fingerprint_helpers")
    # Imports the helper block expects from the surrounding module.
    mod.__dict__.update({
        "base64": base64,
        "hashlib": hashlib,
        "json": json,
        "math": math,
        "re": re,
        "struct": struct,
    })
    try:
        from importlib import metadata as _ilmd
        mod._ilmd = _ilmd  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover
        mod._ilmd = None  # type: ignore[attr-defined]
    exec(compile(block, str(RUN_JSON), "exec"), mod.__dict__)
    return mod


def _load_runtime():
    """Import the full harness without invoking its CLI main()."""
    spec = importlib.util.spec_from_file_location(
        "pf_release_run_json", RUN_JSON)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {RUN_JSON}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    m = _load_helpers()
    runtime = _load_runtime()
    passed = failed = 0

    def check(name: str, cond: bool) -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  OK   {name}")
        else:
            failed += 1
            print(f"  FAIL {name}")

    # --- integral floats: no trailing .0 (live MCP bug) -----------------
    for value, expected in ((0.0, "0"), (1.0, "1"), (12345.0, "12345"),
                            (-0.0, "0"), (-12345.0, "-12345")):
        got = m._canonical_fingerprint_json(value)
        check(f"integral float {value!r} -> {expected!r}", got == expected)

    live = {"strategy": {"initial_capital": 12345.0,
                         "commission_value": 0.0,
                         "default_qty_value": 1.0}}
    live_canon = m._canonical_fingerprint_json(live)
    check("live nested integral floats have no trailing .0",
          live_canon == ('{"strategy":{"commission_value":0,'
                         '"default_qty_value":1,'
                         '"initial_capital":12345}}')
          and "12345.0" not in live_canon
          and "0.0" not in live_canon
          and "1.0" not in live_canon)

    # --- token digest hashes exact decoded bytes -----------------------
    fp = m.build_fingerprint(live)
    token_bytes = base64.b64decode(fp["token"])
    check("token decodes to canonical UTF-8 bytes",
          token_bytes == live_canon.encode("utf-8"))
    check("digest is sha256 of exact decoded token bytes",
          fp["digest"]
          == "sha256:" + hashlib.sha256(token_bytes).hexdigest())
    check("digest prefix", fp["digest"].startswith("sha256:"))
    check("deterministic fingerprint",
          m.build_fingerprint(live)["token"] == fp["token"]
          and m.build_fingerprint(live)["digest"] == fp["digest"])

    # --- source tape identity is part of authoritative provenance -------
    source_sha = "a" * 64
    provenance = m.build_provenance(
        {"version_string": "0.12.2"}, None, True, {}, {}, {},
        source_feed_sha256=source_sha)
    check("source feed identity is included",
          provenance["feed"] == {
              "canonicalization": "pf-ohlcv-barc-le-v1",
              "source_values_sha256": source_sha,
          })
    bad_source_sha_rejected = False
    try:
        m.build_provenance(
            {}, None, False, {}, {}, {}, source_feed_sha256="not-a-sha")
    except ValueError:
        bad_source_sha_rejected = True
    check("invalid source feed identity fails closed", bad_source_sha_rejected)

    with tempfile.TemporaryDirectory() as td:
        tape_a = Path(td) / "a.csv"
        tape_b = Path(td) / "b.csv"
        header = "open,high,low,close,volume,timestamp\n"
        tape_a.write_text(
            header
            + "1,2,0.5,1.5,10,1000\n"
            + "1.5,3,1,2.5,20,2000\n",
            encoding="utf-8")
        tape_b.write_text(
            header
            + "1,2,0.5,1.5,10,1000\n"
            + "1.5,3,1,2.6,20,2000\n",
            encoding="utf-8")
        bars_a, count_a, feed_a = runtime.load_bars(tape_a)
        _, count_b, feed_b = runtime.load_bars(tape_b)
        expected_feed = hashlib.sha256()
        expected_feed.update(b"pineforge:ohlcv:barc-le:v1\0")
        expected_feed.update(struct.pack(
            "<5dq", 1.0, 2.0, 0.5, 1.5, 10.0, 1000))
        expected_feed.update(struct.pack(
            "<5dq", 1.5, 3.0, 1.0, 2.5, 20.0, 2000))
        check("CSV loader emits canonical source feed identity",
              count_a == count_b == 2
              and bars_a[1].close == 2.5
              and feed_a == expected_feed.hexdigest())
        check("changed source value changes feed identity", feed_a != feed_b)
        provenance_a = runtime.build_provenance(
            {}, None, False, {}, {}, {}, source_feed_sha256=feed_a)
        provenance_b = runtime.build_provenance(
            {}, None, False, {}, {}, {}, source_feed_sha256=feed_b)
        check("changed source tape changes run fingerprint",
              runtime.build_fingerprint(provenance_a)["digest"]
              != runtime.build_fingerprint(provenance_b)["digest"])

    trades = (runtime.TradeC * 2)()
    trades[0].is_long = 1
    trades[1].is_long = 0
    report = runtime.ReportC()
    report.trades = ctypes.cast(
        trades, ctypes.POINTER(runtime.TradeC))
    report.trades_len = 2
    rendered = runtime.build_report_dict(
        report, Path("fixture.csv"), 2, 1000, 2000, 0.0, {}, {},
        trade_entry_incarnations=[41])
    check("entry incarnation aligns by trade with guarded fallback",
          rendered["trades"][0]["entry_incarnation"] == 41
          and rendered["trades"][1]["entry_incarnation"] == 0)

    # --- UTF-16 object-key order + raw Unicode --------------------------
    chinese = m._canonical_fingerprint_json({"中文键": "中文值"})
    check("Chinese object is raw Unicode JSON",
          chinese == '{"中文键":"中文值"}' and "\\u" not in chinese)

    emoji = m._canonical_fingerprint_json("😀")
    check("emoji is raw Unicode", emoji == '"😀"' and "\\u" not in emoji)

    seps = m._canonical_fingerprint_json("\u2028\u2029")
    check("U+2028/U+2029 raw (not ensure_ascii)",
          seps == '"\u2028\u2029"' and "\\u2028" not in seps)

    # U+10000 sorts *before* U+E000 under UTF-16 code units (JCS/JS),
    # but *after* under Unicode code-point order (Python sorted()).
    key_order = m._canonical_fingerprint_json({"\U00010000": 1, "\uE000": 2})
    check("UTF-16 key order U+10000 before U+E000",
          key_order == '{"\U00010000":1,"\uE000":2}')
    check("UTF-16 order differs from code-point order",
          sorted(["\U00010000", "\uE000"]) == ["\uE000", "\U00010000"])

    special = m._canonical_fingerprint_json(
        {"2": 2, "10": 10, "__proto__": "own"})
    check("special keys 10/2/__proto__ lexical UTF-16 order",
          special == '{"10":10,"2":2,"__proto__":"own"}')

    # --- fail closed: nonfinite, surrogates, unsafe ints, dup keys -----
    nonfinite_ok = True
    for bad in (float("nan"), float("inf"), float("-inf")):
        try:
            m._canonical_fingerprint_json({"x": bad})
            nonfinite_ok = False
        except ValueError:
            pass
    check("non-finite numbers rejected", nonfinite_ok)

    surrogate_ok = True
    for bad in ("\ud800", "\udfff", "a\ud800b"):
        try:
            m._canonical_fingerprint_json(bad)
            surrogate_ok = False
        except ValueError:
            pass
        try:
            m._canonical_fingerprint_json({bad: "x"})
            surrogate_ok = False
        except ValueError:
            pass
    check("unpaired surrogates rejected", surrogate_ok)

    unsafe_ok = True
    for bad in (9007199254740992,  # exact int 2**53
                9007199254740993,
                -9007199254740992,
                9223372036854775807):
        try:
            m._canonical_fingerprint_json(bad)
            unsafe_ok = False
        except ValueError:
            pass
        try:
            m.build_fingerprint({"n": bad})
            unsafe_ok = False
        except ValueError:
            pass
    check("unsafe exact ints rejected (safe-integer domain)", unsafe_ok)
    # Isolated binary64 float(2**53) remains legal on the float path.
    check("float 2**53 remains legal binary64 token",
          m._canonical_fingerprint_json(9007199254740992.0)
          == "9007199254740992")

    tagged_a = _TaggedStr("same", "a")
    tagged_b = _TaggedStr("same", "b")
    tagged_dup = {tagged_a: 1, tagged_b: 2}
    check("tagged str keys coexist pre-normalization",
          len(tagged_dup) == 2
          and str.__str__(tagged_a) == str.__str__(tagged_b) == "same")
    dup_rejected = False
    try:
        m._canonical_fingerprint_json(tagged_dup)
    except ValueError:
        dup_rejected = True
    check("duplicate normalized str-subclass keys fail closed", dup_rejected)

    # --- direct Node ECMAScript / JCS reconstruction (required) --------
    node = shutil.which("node")
    if node is None:
        check("Node available for JCS bridge", False)
        print("  (install Node.js to run the ECMAScript/JCS bridge)")
    else:
        bridge_payload = {
            "strategy": {
                "initial_capital": 12345.0,
                "commission_value": 0.0,
                "default_qty_value": 1.0,
            },
            "nested": {"arr": [0.0, -0.0, 1.0, 12345.0]},
            "中文键": "中文值",
            "emoji": "😀",
            "sep": "\u2028\u2029",
            "\U00010000": "supplementary",
            "\uE000": "pua",
            "10": 10,
            "2": 2,
            "__proto__": "own",
        }
        py_canon = m._canonical_fingerprint_json(bridge_payload)
        # Non-canonical Python JSON a Worker would re-canonicalize.
        py_legacy = json.dumps(bridge_payload, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=True)
        script = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
// RFC 8785/JCS direct canonical writer over JS-parsed values.
// Do NOT rebuild a plain object (array-index key reorder; __proto__ drop).
function canonical(v) {
  if (Array.isArray(v)) {
    let s = '[';
    for (let i = 0; i < v.length; i++) {
      if (i) s += ',';
      s += canonical(v[i]);
    }
    return s + ']';
  }
  if (v !== null && typeof v === 'object') {
    const keys = Object.keys(v).sort();
    let s = '{';
    for (let i = 0; i < keys.length; i++) {
      const k = keys[i];
      if (i) s += ',';
      s += JSON.stringify(k) + ':' + canonical(v[k]);
    }
    return s + '}';
  }
  return JSON.stringify(v);
}
const fromLegacy = canonical(JSON.parse(input.legacy));
const fromCanon = canonical(JSON.parse(input.canonical));
process.stdout.write(JSON.stringify({fromLegacy, fromCanon}));
"""
        proc = subprocess.run(
            [node, "-e", script],
            input=json.dumps({
                "legacy": py_legacy,
                "canonical": py_canon,
            }, ensure_ascii=False),
            text=True, capture_output=True, check=False)
        if proc.returncode != 0:
            check("Node JCS bridge executes", False)
            print(proc.stderr)
        else:
            out = json.loads(proc.stdout)
            check("Node direct JCS reconstruction is byte-identical",
                  out["fromLegacy"] == py_canon
                  and out["fromCanon"] == py_canon)
            js_digest = ("sha256:"
                         + hashlib.sha256(
                             out["fromLegacy"].encode("utf-8")).hexdigest())
            check("Node-emitted bytes hash to the same digest",
                  js_digest == m.build_fingerprint(bridge_payload)["digest"])
            check("bridge payload retains special keys + UTF-16 order",
                  '"10":10' in py_canon
                  and '"2":2' in py_canon
                  and '"__proto__":"own"' in py_canon
                  and py_canon.index('"\U00010000"')
                  < py_canon.index('"\uE000"')
                  and "中文键" in py_canon
                  and "\\u4e2d" not in py_canon)

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
