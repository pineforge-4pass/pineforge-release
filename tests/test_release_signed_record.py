#!/usr/bin/env python3
"""Regressions for scripts/release_signed_record.py (application-only signed records).

Evidence label: test-key vectors, not a live receipt proof.

The module never supplies its own expected values. Canonical bytes come from the pinned
vector files (derived from the App's canonical()) and from literals typed in this file.
Signatures come from an ephemeral key made by the cryptography library inside a test, or
from the generated test-key fixture. The fixture signed bytes are generated on an approved
Spot box; until then PreSignedVectorsTest fails by name instead of passing.
"""
from __future__ import annotations

import base64
import collections
import copy
import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_signed_record as signed  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "signed-record"
SPEC = ROOT / "docs" / "release-signed-record.md"
MODULE = ROOT / "scripts" / "release_signed_record.py"
LABEL = "test-key vectors, not a live receipt proof"
# The key pinned in hub policy by the SHA-256 of its DER SubjectPublicKeyInfo. No test key may satisfy it.
LIVE_PIN = "520945039e0d2d4b3e28f9e9fb87045af79c4358479677effad6b427ea7c7b71"
LIVE_PUBLIC_PEM = (
    b"-----BEGIN PUBLIC KEY-----\n"
    b"MCowBQYDK2VwAyEAmCRX+MiFU4X3w7IstkIvlejwIPdI7+Ak6hn4rWpm36s=\n"
    b"-----END PUBLIC KEY-----\n"
)
APP_CANONICAL_BLOB = "6520023f1b6f4ef5af6bb8ffab064a52cd755616"
APP_SIGNED_STATE_BLOB = "260912eb7255d7ac59e6867df94e34bc94a4c00f"

E_ACUTE = chr(0xE9)
COMBINING_ACUTE = chr(0x301)
RECORD = {"schema": "pineforge/dev-observation/v1", "owner": 1234567890, "kind": "receipt-run",
          "note": "caf" + E_ACUTE, "pair": None}
# Typed by hand from the App's canonical(): keys sorted, compact, LF, UTF-8. Not produced by the module.
RECORD_BYTES = ('{"kind":"receipt-run","note":"caf' + E_ACUTE + '","owner":1234567890,"pair":null,'
                '"schema":"pineforge/dev-observation/v1"}\n').encode("utf-8")
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


class _Text(str):
    pass


class _Number(int):
    pass


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _spki(private_key):
    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return signed.SPKI_PREFIX + raw


def _trusted(spki):
    return signed.load_trusted_key(spki, expected_spki_sha256=hashlib.sha256(spki).hexdigest())


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _envelope(record, private_key, message=RECORD_BYTES):
    return {"record": record, "signature": _b64(private_key.sign(message))}


def _refused(test, code, func, *args, **kwargs):
    with test.assertRaises(signed.SignedRecordError) as caught:
        func(*args, **kwargs)
    test.assertEqual(caught.exception.code, code)
    return caught.exception


def _deep(levels, container):
    value = container()
    for _ in range(levels - 1):
        value = [value] if container is list else {"a": value}
    return value


class CanonicalVectorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.file = _load("canonical-vectors.json")

    def test_file_is_labelled_and_pinned_to_the_app_source(self):
        self.assertEqual(self.file["label"], LABEL)
        self.assertEqual(self.file["pinned_app_source"]["canonical"]["git_blob"], APP_CANONICAL_BLOB)

    def test_both_fixture_files_are_pure_ascii(self):
        # Non-ASCII characters are JSON escapes, so no editor can normalize an NFC or NFD vector.
        for name in ("canonical-vectors.json", "signature-vectors.json"):
            with self.subTest(file=name):
                self.assertTrue((FIXTURES / name).read_bytes().isascii())

    def test_each_vector_is_well_formed_and_its_two_pins_agree(self):
        ids = [vector["id"] for vector in self.file["vectors"]]
        self.assertEqual(len(ids), len(set(ids)))
        for vector in self.file["vectors"]:
            with self.subTest(vector=vector["id"]):
                self.assertIn(vector["app"]["result"], ("canonical", "throws"))
                if vector["hub"]["result"] == "accept":
                    self.assertEqual(vector["app"]["result"], "canonical")
                    self.assertTrue(vector["app"]["text"].endswith("\n"))
                    self.assertRegex(vector["utf8_hex"], r"\A(?:[0-9a-f]{2})+\Z")
                    self.assertEqual(bytes.fromhex(vector["utf8_hex"]).decode("utf-8"), vector["app"]["text"])
                else:
                    self.assertEqual(vector["hub"]["result"], "reject")
                    self.assertIn(vector["hub"]["code"], signed.REJECT_CODES)
                    self.assertNotIn("utf8_hex", vector)

    def test_every_vector_is_decided_as_pinned(self):
        for vector in self.file["vectors"]:
            with self.subTest(vector=vector["id"]):
                raw = vector["input_json"].encode("utf-8")
                if vector["hub"]["result"] == "accept":
                    value = signed.parse_json_strict(raw)
                    self.assertEqual(signed.canonical_bytes(value), bytes.fromhex(vector["utf8_hex"]))
                    self.assertEqual(signed.canonical_bytes(value), vector["app"]["text"].encode("utf-8"))
                    self.assertEqual(signed.canonical_text(value), vector["app"]["text"])
                else:
                    _refused(self, vector["hub"]["code"], signed.parse_json_strict, raw)

    def test_paired_vectors_sign_the_bytes_they_claim_to(self):
        by_id = {vector["id"]: vector for vector in self.file["vectors"]}
        differ = (("unicode-nfc-not-normalized", "unicode-nfd-not-normalized"),
                  ("null-versus-absent-null-key", "null-versus-absent-missing-key"))
        for first, second in differ:
            with self.subTest(pair=(first, second)):
                self.assertNotEqual(by_id[first]["utf8_hex"], by_id[second]["utf8_hex"])
        self.assertEqual(by_id["string-unicode-raw"]["utf8_hex"], by_id["string-unicode-escaped-equals-raw"]["utf8_hex"])


class CanonicalBytesTest(unittest.TestCase):
    def test_insertion_order_does_not_change_the_bytes(self):
        forward = {"b": 1, "a": 2, "10": 3, "2": 4}
        backward = {"2": 4, "10": 3, "a": 2, "b": 1}
        self.assertEqual(signed.canonical_bytes(forward), b'{"2":4,"10":3,"a":2,"b":1}\n')
        self.assertEqual(signed.canonical_bytes(backward), b'{"2":4,"10":3,"a":2,"b":1}\n')

    def test_bool_and_int_stay_distinct_and_any_in_domain_value_has_a_canonical_form(self):
        self.assertEqual(signed.canonical_bytes({"a": True}), b'{"a":true}\n')
        self.assertEqual(signed.canonical_bytes({"a": 1}), b'{"a":1}\n')
        self.assertEqual(signed.canonical_bytes([False, 0, None, ""]), b'[false,0,null,""]\n')
        self.assertEqual(signed.canonical_bytes(5), b"5\n")
        self.assertEqual(signed.canonical_bytes("x"), b'"x"\n')
        self.assertEqual(signed.canonical_bytes([]), b"[]\n")
        self.assertEqual(signed.canonical_bytes(None), b"null\n")

    def test_values_outside_the_domain_are_refused_by_type(self):
        values = (("float", 1.5), ("integral-float", 1.0), ("nan", float("nan")), ("infinity", float("inf")),
                  ("tuple", (1, 2)), ("bytes", b"x"), ("set", {1}), ("complex", 1j), ("object", object()),
                  ("dict-subclass", collections.OrderedDict(a=1)), ("str-subclass", _Text("x")),
                  ("int-subclass", _Number(5)))
        for name, value in values:
            with self.subTest(value=name):
                _refused(self, "unsupported_type", signed.canonical_bytes, value)
                _refused(self, "unsupported_type", signed.canonical_bytes, {"a": [value]})
        for key in (1, None, ("a",), b"a", _Text("a")):
            with self.subTest(key=key):
                _refused(self, "unsupported_type", signed.canonical_bytes, {key: 1})

    def test_integer_range_edges(self):
        edge = 2**53 - 1
        self.assertEqual(signed.canonical_bytes([edge, -edge]), b"[9007199254740991,-9007199254740991]\n")
        for name, value in (("first-unsafe", edge + 1), ("first-unsafe-negative", -edge - 1),
                            ("five-thousand-digits", 10**5000)):
            with self.subTest(value=name):
                _refused(self, "number_range", signed.canonical_bytes, [value])

    def test_unpaired_surrogates_are_refused_in_values_and_keys(self):
        high, low = chr(0xD83D), chr(0xDE00)
        for text in (chr(0xD800), chr(0xDC00), "a" + high + "b", low + high, high + low):
            with self.subTest(text=ascii(text)):
                _refused(self, "unpaired_surrogate", signed.canonical_bytes, [text])
                _refused(self, "unpaired_surrogate", signed.canonical_bytes, {text: 1})
        emoji = chr(0x1F600)
        self.assertEqual(signed.canonical_bytes(emoji), ('"' + emoji + '"\n').encode("utf-8"))

    def test_index_like_key_rules(self):
        self.assertEqual(signed.canonical_bytes({"b": 1, "4294967294": 2, "0": 3}), b'{"0":3,"4294967294":2,"b":1}\n')
        for key in ("4294967295", "9007199254740993", "1" * 5000):
            with self.subTest(key=key[:12]):
                _refused(self, "ambiguous_index_key", signed.canonical_bytes, {key: 1})
        fullwidth_one = chr(0xFF11)
        ordinary = {"00": 1, "-0": 2, "1.5": 3, " 1": 4, fullwidth_one: 5}
        self.assertEqual(signed.canonical_bytes(ordinary),
                         ('{" 1":4,"-0":2,"00":1,"1.5":3,"' + fullwidth_one + '":5}\n').encode("utf-8"))

    def test_nesting_limit(self):
        for container in (list, dict):
            with self.subTest(container=container.__name__):
                signed.canonical_bytes(_deep(signed.MAX_DEPTH, container))
                _refused(self, "depth_exceeded", signed.canonical_bytes, _deep(signed.MAX_DEPTH + 1, container))


class ParseJsonStrictTest(unittest.TestCase):
    def test_accepts_plain_json_in_the_domain(self):
        text = b' \t\r\n{"a":[1,-2,true,null,"x\\u00e9"],"b":{}} \n'
        value = signed.parse_json_strict(text)
        self.assertEqual(value, {"a": [1, -2, True, None, "x" + E_ACUTE], "b": {}})
        self.assertIs(type(value["a"][2]), bool)
        self.assertEqual(signed.parse_json_strict(b"[1,2]"), [1, 2])
        self.assertEqual(signed.parse_json_strict(b"null"), None)
        self.assertEqual(signed.parse_json_strict(b"{}" + b" " * (signed.MAX_ENVELOPE_BYTES - 2)), {})

    def test_refusals_carry_their_own_cause(self):
        cases = (
            ("str-input", "input_type", "{}"),
            ("bytearray-input", "input_type", bytearray(b"{}")),
            ("none-input", "input_type", None),
            ("too-large", "too_large", b" " * (signed.MAX_ENVELOPE_BYTES + 1)),
            ("bom", "bom", b"\xef\xbb\xbf{}"),
            ("lone-continuation-byte", "bad_utf8", b'{"a":"\x80"}'),
            ("encoded-surrogate", "bad_utf8", b'{"a":"\xed\xa0\x80"}'),
            ("overlong-slash", "bad_utf8", b'{"a":"\xc0\xaf"}'),
            ("utf16-with-bom", "bad_utf8", "{}".encode("utf-16")),
            ("empty", "json_syntax", b""),
            ("whitespace-only", "json_syntax", b"  \n"),
            ("trailing-data", "json_syntax", b"{} {}"),
            ("trailing-comma", "json_syntax", b'{"a":1,}'),
            ("single-quotes", "json_syntax", b"{'a':1}"),
            ("comment", "json_syntax", b'{"a":1}//x'),
            ("leading-zero", "json_syntax", b'{"a":01}'),
            ("plus-sign", "json_syntax", b'{"a":+1}'),
            ("bare-fraction", "json_syntax", b'{"a":.5}'),
            ("raw-control-character", "json_syntax", b'{"a":"\x01"}'),
            ("unterminated", "json_syntax", b'{"a":'),
            ("nan", "nonfinite_constant", b'{"a":NaN}'),
            ("infinity", "nonfinite_constant", b'{"a":Infinity}'),
            ("negative-infinity", "nonfinite_constant", b'{"a":-Infinity}'),
            ("duplicate", "duplicate_key", b'{"a":1,"a":1}'),
            ("duplicate-after-unescape", "duplicate_key", b'{"a":1,"\\u0061":2}'),
            ("duplicate-nested", "duplicate_key", b'{"x":{"a":1,"a":2}}'),
            ("duplicate-in-array", "duplicate_key", b'[{"a":1,"a":2}]'),
            ("decimal-point", "number_form", b'{"a":1.0}'),
            ("exponent", "number_form", b'{"a":1e2}'),
            ("upper-exponent", "number_form", b'{"a":1E2}'),
            ("signed-exponent", "number_form", b'{"a":1e+2}'),
            ("negative-zero", "number_form", b'{"a":-0}'),
            ("negative-zero-fraction", "number_form", b'{"a":-0.0}'),
            ("fraction", "number_form", b'{"a":0.5}'),
            ("overflow", "number_form", b'{"a":1e999}'),
            ("tiny-exponent", "number_form", b'{"a":1.5e-3}'),
            ("first-unsafe", "number_range", b'{"a":9007199254740992}'),
            ("first-unsafe-negative", "number_range", b'{"a":-9007199254740992}'),
            ("seventeen-digits", "number_range", b'{"a":12345678901234567}'),
            ("five-thousand-digits", "number_range", b'{"a":1' + b"0" * 5000 + b"}"),
            ("nesting-33", "depth_exceeded", b"[" * (signed.MAX_DEPTH + 1) + b"]" * (signed.MAX_DEPTH + 1)),
            ("nesting-100", "depth_exceeded", b"[" * 100 + b"]" * 100),
            ("nesting-beyond-the-parser", "depth_exceeded", b"[" * 5000 + b"]" * 5000),
            ("lone-surrogate-escape", "unpaired_surrogate", b'"\\ud800"'),
            ("lone-surrogate-key-escape", "unpaired_surrogate", b'{"\\udc00":1}'),
            ("index-key", "ambiguous_index_key", b'{"4294967295":1}'),
        )
        for name, code, raw in cases:
            with self.subTest(input=name):
                _refused(self, code, signed.parse_json_strict, raw)

    def test_the_nesting_boundary_is_exactly_max_depth(self):
        ok = b"[" * signed.MAX_DEPTH + b"]" * signed.MAX_DEPTH
        value = signed.parse_json_strict(ok)
        self.assertEqual(signed.canonical_bytes(value), ok + b"\n")


class TrustedKeyTest(unittest.TestCase):
    def test_spki_prefix_matches_the_librarys_own_der_encoding(self):
        public = Ed25519PrivateKey.generate().public_key()
        der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.assertEqual(der, signed.SPKI_PREFIX + raw)
        self.assertEqual(len(der), signed.SPKI_BYTES)

    def test_loads_a_key_only_against_its_own_pin(self):
        spki = _spki(Ed25519PrivateKey.generate())
        pin = hashlib.sha256(spki).hexdigest()
        loaded = signed.load_trusted_key(spki, expected_spki_sha256=pin)
        self.assertIs(type(loaded), signed.TrustedKey)
        self.assertEqual(loaded.spki_sha256, pin)
        other = hashlib.sha256(spki + b"x").hexdigest()
        _refused(self, "key_pin_mismatch", signed.load_trusted_key, spki, expected_spki_sha256=other)

    def test_a_test_key_never_satisfies_the_live_pin(self):
        for _ in range(3):
            spki = _spki(Ed25519PrivateKey.generate())
            _refused(self, "key_pin_mismatch", signed.load_trusted_key, spki, expected_spki_sha256=LIVE_PIN)

    def test_pin_format_is_exact(self):
        spki = _spki(Ed25519PrivateKey.generate())
        for pin in ("", LIVE_PIN.upper(), LIVE_PIN[:-1], LIVE_PIN + "0", "g" * 64, " " + LIVE_PIN[1:],
                    LIVE_PIN + "\n", None, 5, LIVE_PIN.encode("ascii")):
            with self.subTest(pin=repr(pin)[:20]):
                _refused(self, "key_pin_format", signed.load_trusted_key, spki, expected_spki_sha256=pin)

    def test_only_the_exact_ed25519_der_form_loads(self):
        spki = _spki(Ed25519PrivateKey.generate())
        bad = (("empty", b""), ("raw-key-only", spki[12:]), ("truncated", spki[:-1]), ("trailing-byte", spki + b"\x00"),
               ("x25519-oid", spki[:8] + b"\x6e" + spki[9:]), ("bit-string-unused-bits", spki[:11] + b"\x01" + spki[12:]),
               ("pem-text", b"-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA\n-----END PUBLIC KEY-----\n"))
        for name, der in bad:
            with self.subTest(key=name):
                _refused(self, "key_encoding", signed.load_trusted_key, der, expected_spki_sha256=LIVE_PIN)

    def test_the_key_must_be_bytes(self):
        spki = _spki(Ed25519PrivateKey.generate())
        for value in (None, spki.hex(), bytearray(spki), memoryview(spki), [spki]):
            with self.subTest(key=type(value).__name__):
                _refused(self, "input_type", signed.load_trusted_key, value, expected_spki_sha256=LIVE_PIN)

    def test_a_key_cannot_be_built_or_changed_outside_load_trusted_key(self):
        private = Ed25519PrivateKey.generate()
        public = private.public_key()
        for token in (None, object(), "loaded", public):
            with self.subTest(token=type(token).__name__):
                with self.assertRaises(TypeError):
                    signed.TrustedKey(token, public, "0" * 64)
        loaded = _trusted(_spki(private))
        for name in ("spki_sha256", "_public_key", "extra"):
            with self.subTest(attribute=name):
                with self.assertRaises(AttributeError):
                    setattr(loaded, name, "x")
        self.assertEqual(loaded.spki_sha256, hashlib.sha256(_spki(private)).hexdigest())


class VerifyEnvelopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.signer = Ed25519PrivateKey.generate()
        cls.other = Ed25519PrivateKey.generate()
        cls.trusted = _trusted(_spki(cls.signer))
        cls.other_trusted = _trusted(_spki(cls.other))

    def verify(self, envelope, kind="receipt-run", trusted=None):
        return signed.verify_envelope(envelope, trusted or self.trusted, expected_kind=kind)

    def test_accepts_the_record_signed_by_the_pinned_key_and_returns_it(self):
        envelope = _envelope(RECORD, self.signer)
        record = self.verify(envelope)
        self.assertIs(record, envelope["record"])
        self.assertEqual(record, RECORD)

    def test_bytes_entry_point_accepts_every_spelling_of_the_same_value(self):
        envelope = _envelope(RECORD, self.signer)
        spellings = {
            "default": json.dumps(envelope),
            "raw-unicode": json.dumps(envelope, ensure_ascii=False),
            "sorted-keys": json.dumps(envelope, sort_keys=True),
            "pretty": json.dumps(envelope, indent=2) + "\n",
            "crlf-and-tabs": json.dumps(envelope, indent="\t").replace("\n", "\r\n"),
            "compact": json.dumps(envelope, separators=(",", ":")),
        }
        for name, text in spellings.items():
            with self.subTest(spelling=name):
                record = signed.verify_envelope_bytes(text.encode("utf-8"), self.trusted, expected_kind="receipt-run")
                self.assertEqual(record, RECORD)

    def test_every_change_of_a_signed_value_is_a_bad_signature(self):
        original = _envelope(RECORD, self.signer)
        changes = (
            ("owner-plus-one", lambda r: r.update(owner=1234567891)),
            ("note-changed", lambda r: r.update(note="cafe")),
            ("note-decomposed", lambda r: r.update(note="cafe" + COMBINING_ACUTE)),
            ("key-added", lambda r: r.update(extra="x")),
            ("null-key-added", lambda r: r.update(extra=None)),
            ("null-key-removed", lambda r: r.pop("pair")),
            ("null-to-false", lambda r: r.update(pair=False)),
            ("null-to-zero", lambda r: r.update(pair=0)),
            ("kind-changed", lambda r: r.update(kind="snapshot")),
        )
        for name, change in changes:
            with self.subTest(change=name):
                mutated = copy.deepcopy(original)
                change(mutated["record"])
                _refused(self, "signature_invalid", self.verify, mutated)

    def test_one_flipped_byte_of_the_envelope_text_is_a_bad_signature(self):
        raw = json.dumps(_envelope(RECORD, self.signer)).encode("utf-8")
        at = raw.index(b"1234567890")
        flipped = raw[:at + 9] + b"1" + raw[at + 10:]
        self.assertEqual(sum(1 for a, b in zip(raw, flipped) if a != b), 1)
        self.assertEqual(len(raw), len(flipped))
        _refused(self, "signature_invalid", signed.verify_envelope_bytes, flipped, self.trusted,
                 expected_kind="receipt-run")

    def test_number_spellings_of_an_unchanged_value_are_a_spelling_refusal_not_a_bad_signature(self):
        envelope = _envelope(RECORD, self.signer)
        raw = json.dumps(envelope).encode("utf-8")
        for name, spelled in (("decimal-point", b"1234567890.0"), ("exponent", b"1.23456789e9"),
                              ("upper-exponent", b"1.23456789E9")):
            with self.subTest(spelling=name):
                variant = raw.replace(b"1234567890", spelled)
                self.assertNotEqual(variant, raw)
                _refused(self, "number_form", signed.verify_envelope_bytes, variant, self.trusted,
                         expected_kind="receipt-run")
        parsed = copy.deepcopy(envelope)
        parsed["record"]["owner"] = 1234567890.0
        _refused(self, "unsupported_type", self.verify, parsed)

    def test_signature_defects_have_their_own_causes(self):
        raw = base64.b64decode(_envelope(RECORD, self.signer)["signature"])
        text = _b64(raw)
        position = len(text) - 3
        value = ALPHABET.index(text[position])
        self.assertEqual(value % 16, 0, "the last data character of a 64-byte signature has four zero low bits")
        s_plus_l = raw[:32] + (int.from_bytes(raw[32:], "little") + signed.GROUP_ORDER).to_bytes(32, "little")
        cases = (
            ("bit-flipped", _b64(bytes([raw[0] ^ 1]) + raw[1:]), "signature_invalid"),
            ("all-zero", _b64(bytes(64)), "signature_invalid"),
            ("short", _b64(raw[:63]), "signature_length"),
            ("long", _b64(raw + b"\x00"), "signature_length"),
            ("empty", "", "signature_length"),
            ("unpadded", text.rstrip("="), "signature_encoding"),
            ("trailing-newline", text + "\n", "signature_encoding"),
            ("inner-space", text[:10] + " " + text[10:], "signature_encoding"),
            ("url-safe-characters", "-" * 86 + "==", "signature_encoding"),
            ("non-ascii", text[:-1] + E_ACUTE, "signature_encoding"),
            ("nonzero-trailing-bits", text[:position] + ALPHABET[value + 1] + text[position + 1:], "signature_encoding"),
            ("s-plus-l", _b64(s_plus_l), "signature_noncanonical"),
        )
        for name, signature, code in cases:
            with self.subTest(signature=name):
                _refused(self, code, self.verify, {"record": RECORD, "signature": signature})
        for value in (None, 7, True, ["x"], {"x": 1}, b"AAAA"):
            with self.subTest(signature_type=repr(value)):
                _refused(self, "signature_type", self.verify, {"record": RECORD, "signature": value})

    def test_envelope_and_record_shapes(self):
        good = _envelope(RECORD, self.signer)
        for name, envelope in (("list", []), ("none", None), ("string", "x"), ("missing-record", {"signature": good["signature"]}),
                               ("extra-public-key", {**good, "public_key": "x"}), ("extra-kid", {**good, "kid": "k"})):
            with self.subTest(envelope=name):
                _refused(self, "envelope_shape", self.verify, envelope)
        _refused(self, "signature_missing", self.verify, {"record": RECORD})
        _refused(self, "signature_missing", self.verify, {})
        for name, record in (("list", []), ("string", "x"), ("none", None), ("int", 5)):
            with self.subTest(record=name):
                _refused(self, "record_shape", self.verify, {"record": record, "signature": good["signature"]})

    def test_a_key_carried_by_the_envelope_or_the_record_is_never_used(self):
        attacker = Ed25519PrivateKey.generate()
        attacker_key = _b64(_spki(attacker))
        forged = _envelope(RECORD, attacker)
        _refused(self, "signature_invalid", self.verify, forged)
        _refused(self, "envelope_shape", self.verify, {**forged, "public_key": attacker_key})
        with_key = dict(RECORD, public_key=attacker_key)
        message = RECORD_BYTES.replace(b'"schema"', b'"public_key":"' + attacker_key.encode("ascii") + b'","schema"')
        vouched = _envelope(with_key, attacker, message)
        self.assertEqual(self.verify(vouched, trusted=_trusted(_spki(attacker))), with_key)
        _refused(self, "signature_invalid", self.verify, vouched)

    def test_kind_is_the_callers_choice_not_the_envelopes(self):
        snapshot = dict(RECORD, kind="snapshot")
        message = RECORD_BYTES.replace(b'"kind":"receipt-run"', b'"kind":"snapshot"')
        proof = _envelope(snapshot, self.signer, message)
        self.assertEqual(self.verify(proof, kind="snapshot"), snapshot)
        _refused(self, "kind_mismatch", self.verify, proof, kind="receipt-run")
        binder = _envelope(RECORD, self.signer)
        _refused(self, "kind_mismatch", self.verify, binder, kind="snapshot")
        no_kind = {key: value for key, value in RECORD.items() if key != "kind"}
        unkinded = _envelope(no_kind, self.signer, RECORD_BYTES.replace(b'"kind":"receipt-run",', b""))
        _refused(self, "kind_mismatch", self.verify, unkinded)
        for kind in ("", None, 5, b"receipt-run", ["receipt-run"]):
            with self.subTest(kind=repr(kind)):
                _refused(self, "input_type", signed.verify_envelope, binder, self.trusted, expected_kind=kind)
                _refused(self, "input_type", signed.verify_envelope_bytes, b"{}", self.trusted, expected_kind=kind)

    def test_schema_and_owner_are_checked_after_the_signature(self):
        no_schema = {key: value for key, value in RECORD.items() if key != "schema"}
        cases = (
            ("schema-v2", dict(RECORD, schema="pineforge/dev-observation/v2"),
             RECORD_BYTES.replace(b"observation/v1", b"observation/v2"), "schema_mismatch"),
            ("schema-missing", no_schema,
             RECORD_BYTES.replace(b',"schema":"pineforge/dev-observation/v1"', b""), "schema_mismatch"),
            ("owner-zero", dict(RECORD, owner=0), RECORD_BYTES.replace(b'"owner":1234567890', b'"owner":0'), "owner_invalid"),
            ("owner-negative", dict(RECORD, owner=-1), RECORD_BYTES.replace(b'"owner":1234567890', b'"owner":-1'), "owner_invalid"),
            ("owner-true", dict(RECORD, owner=True), RECORD_BYTES.replace(b'"owner":1234567890', b'"owner":true'), "owner_invalid"),
            ("owner-string", dict(RECORD, owner="7"), RECORD_BYTES.replace(b'"owner":1234567890', b'"owner":"7"'), "owner_invalid"),
            ("owner-null", dict(RECORD, owner=None), RECORD_BYTES.replace(b'"owner":1234567890', b'"owner":null'), "owner_invalid"),
        )
        for name, record, message, code in cases:
            with self.subTest(case=name):
                _refused(self, code, self.verify, _envelope(record, self.signer, message))
                unsigned = _envelope(record, self.signer)
                _refused(self, "signature_invalid", self.verify, unsigned)

    def test_only_a_loaded_trusted_key_verifies(self):
        envelope = _envelope(RECORD, self.signer)
        raw = json.dumps(envelope).encode("utf-8")

        class Lookalike:
            spki_sha256 = "0" * 64
            _public_key = self.signer.public_key()

        keys = (("spki-bytes", _spki(self.signer)), ("public-key-object", self.signer.public_key()),
                ("private-key-object", self.signer), ("none", None), ("pem-bytes", b"-----BEGIN PUBLIC KEY-----"),
                ("mapping", {"spki": _spki(self.signer)}), ("lookalike", Lookalike()))
        for name, key in keys:
            with self.subTest(key=name):
                _refused(self, "key_untrusted", signed.verify_envelope, envelope, key, expected_kind="receipt-run")
                _refused(self, "key_untrusted", signed.verify_envelope_bytes, raw, key, expected_kind="receipt-run")

    def test_the_decision_follows_the_key_and_the_record(self):
        envelope = _envelope(RECORD, self.signer)
        self.assertEqual(self.verify(envelope), RECORD)
        _refused(self, "signature_invalid", self.verify, envelope, trusted=self.other_trusted)
        other_envelope = _envelope(RECORD, self.other)
        self.assertEqual(self.verify(other_envelope, trusted=self.other_trusted), RECORD)
        _refused(self, "signature_invalid", self.verify, other_envelope)

    def test_bytes_and_parsed_entry_points_agree(self):
        envelope = _envelope(RECORD, self.signer)
        raw = json.dumps(envelope).encode("utf-8")
        self.assertEqual(signed.verify_envelope_bytes(raw, self.trusted, expected_kind="receipt-run"),
                         self.verify(json.loads(raw)))
        _refused(self, "duplicate_key", signed.verify_envelope_bytes,
                 raw[:-1] + b',"record":{}}', self.trusted, expected_kind="receipt-run")

    def test_the_nesting_limit_counts_the_envelope_in_both_entry_points(self):
        # Envelope = level 1, record = level 2, so a list value inside the record may nest
        # MAX_DEPTH - 2 levels. The signed bytes are typed here, not produced by the module.
        for levels, accepted in ((signed.MAX_DEPTH - 2, True), (signed.MAX_DEPTH - 1, False)):
            record = dict(RECORD, deep=_deep(levels, list))
            message = b'{"deep":' + b"[" * levels + b"]" * levels + b"," + RECORD_BYTES[1:]
            envelope = _envelope(record, self.signer, message)
            raw = json.dumps(envelope).encode("utf-8")
            with self.subTest(levels=levels):
                if accepted:
                    self.assertEqual(self.verify(envelope), record)
                    self.assertEqual(
                        signed.verify_envelope_bytes(raw, self.trusted, expected_kind="receipt-run"), record)
                else:
                    _refused(self, "depth_exceeded", self.verify, envelope)
                    _refused(self, "depth_exceeded", signed.verify_envelope_bytes, raw, self.trusted,
                             expected_kind="receipt-run")


class PreSignedVectorsTest(unittest.TestCase):
    """The generated test-key fixture. Fails by name until the Spot generator has filled it."""

    @classmethod
    def setUpClass(cls):
        cls.file = _load("signature-vectors.json")
        if cls.file["generation"]["status"] != "generated":
            raise AssertionError("signed bytes are pending Spot generation: tests/fixtures/signed-record/"
                                 "signature-vectors.json has status " + repr(cls.file["generation"]["status"]))
        cls.keys = {}
        for name, entry in cls.file["test_keys"].items():
            private = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(entry["seed_phrase"].encode("utf-8")).digest())
            cls.keys[name] = {"private": private, "spki": _spki(private)}

    def test_label_and_pinned_sources(self):
        self.assertEqual(self.file["label"], LABEL)
        self.assertIn(APP_CANONICAL_BLOB, self.file["generation"]["signer"])
        self.assertIn(APP_SIGNED_STATE_BLOB, self.file["generation"]["signer"])

    def test_recorded_public_keys_are_the_seed_derived_test_keys_and_never_the_live_key(self):
        self.assertNotEqual(self.keys["signer"]["spki"], self.keys["other"]["spki"])
        for name, key in self.keys.items():
            with self.subTest(key=name):
                recorded = base64.b64decode(self.file["test_keys"][name]["spki_der_base64"], validate=True)
                self.assertEqual(recorded, key["spki"])
                self.assertNotEqual(hashlib.sha256(key["spki"]).hexdigest(), LIVE_PIN)
                _refused(self, "key_pin_mismatch", signed.load_trusted_key, key["spki"], expected_spki_sha256=LIVE_PIN)

    def test_base_bytes_agree_three_independent_ways(self):
        base = self.file["base"]
        pinned = bytes.fromhex(base["canonical_utf8_hex"])
        self.assertEqual(pinned, base["canonical_text"].encode("utf-8"))
        value = signed.parse_json_strict(base["record_json"].encode("utf-8"))
        self.assertEqual(signed.canonical_bytes(value), pinned)
        regenerated = self.keys["signer"]["private"].sign(pinned)
        self.assertEqual(regenerated, base64.b64decode(base["signature_base64"], validate=True))

    def test_signature_tokens_were_filled_from_the_right_key_and_bytes(self):
        base = self.file["base"]
        pinned = bytes.fromhex(base["canonical_utf8_hex"])
        other = _b64(self.keys["other"]["private"].sign(pinned))
        for case in self.file["cases"]:
            if case.get("template") is not None or case.get("sign_case_record"):
                continue
            with self.subTest(case=case["id"]):
                try:
                    envelope = json.loads(case["envelope_json"])
                except ValueError:
                    continue
                self.assertEqual(envelope["signature"], base["signature_base64"])
        by_id = {case["id"]: case for case in self.file["cases"]}
        self.assertEqual(json.loads(by_id["refuse-wrong-key-signature"]["envelope_json"])["signature"], other)

    def test_every_case_is_decided_as_pinned(self):
        for case in self.file["cases"]:
            with self.subTest(case=case["id"]):
                trusted = _trusted(self.keys[case["trusted_key"]]["spki"])
                raw = case["envelope_json"].encode("utf-8")
                if case["hub"]["result"] == "accept":
                    record = signed.verify_envelope_bytes(raw, trusted, expected_kind=case["expected_kind"])
                    self.assertEqual(record, json.loads(case["record_text"]))
                    self.assertEqual(record["kind"], case["expected_kind"])
                else:
                    _refused(self, case["hub"]["code"], signed.verify_envelope_bytes, raw, trusted,
                             expected_kind=case["expected_kind"])

    def test_a_spelling_change_is_never_called_a_bad_signature_and_a_value_change_always_is(self):
        base_value = json.loads(self.file["base"]["record_json"])
        compared = 0
        for case in self.file["cases"]:
            if case["mutates"] == "record-representation":
                with self.subTest(case=case["id"]):
                    self.assertNotEqual(case["hub"].get("code"), "signature_invalid")
                    try:
                        value = json.loads(case["record_text"])
                    except ValueError:
                        continue
                    self.assertEqual(value, base_value)
                    compared += 1
            elif case["mutates"] == "record-value":
                with self.subTest(case=case["id"]):
                    self.assertEqual(case["hub"], {"result": "reject", "code": "signature_invalid"})
                    self.assertNotEqual(json.loads(case["record_text"]), base_value)
                    compared += 1
        self.assertGreaterEqual(compared, 14)

    def test_required_mutation_classes_are_present_and_registered(self):
        tags = {tag for case in self.file["cases"] for tag in case["covers"]}
        required = {"unicode-escaping", "number-forms", "key-ordering", "null-handling", "one-byte-flip",
                    "bad-signature", "missing-signature", "wrong-key", "kind-confusion", "key-substitution"}
        self.assertLessEqual(required, tags)
        ids = [case["id"] for case in self.file["cases"]]
        self.assertEqual(len(ids), len(set(ids)))
        for case in self.file["cases"]:
            if case["hub"]["result"] == "reject":
                self.assertIn(case["hub"]["code"], signed.REJECT_CODES)
            self.assertIn(case["app"]["result"], ("accept", "reject", "unspecified"))


class SpecTest(unittest.TestCase):
    def test_supplied_public_key_matches_the_reviewed_der_pin(self):
        key = serialization.load_pem_public_key(LIVE_PUBLIC_PEM)
        spki = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.assertEqual(hashlib.sha256(spki).hexdigest(), LIVE_PIN)
        signed.load_trusted_key(spki, expected_spki_sha256=LIVE_PIN)

    def test_every_reject_code_is_used_by_the_module_and_documented(self):
        source = MODULE.read_text(encoding="utf-8")
        spec = SPEC.read_text(encoding="utf-8")
        used = set(re.findall(r'_refuse\("([^"\n]+)"', source))
        self.assertEqual(used, set(signed.REJECT_CODES))
        self.assertEqual(len(signed.REJECT_CODES), len(set(signed.REJECT_CODES)))
        for code in signed.REJECT_CODES:
            with self.subTest(code=code):
                self.assertIn("`" + code + "`", spec)

    def test_spec_names_the_pinned_sources_and_the_live_pin(self):
        spec = SPEC.read_text(encoding="utf-8")
        for pinned in (APP_CANONICAL_BLOB, APP_SIGNED_STATE_BLOB, LIVE_PIN, LABEL):
            with self.subTest(pinned=pinned):
                self.assertIn(pinned, spec)


if __name__ == "__main__":
    unittest.main()
