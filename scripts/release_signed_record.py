#!/usr/bin/env python3
"""Application-only signed records: App-compatible canonical bytes and Ed25519 verification.

Pinned to the App sources (git blob ids, written spec: docs/release-signed-record.md):
  canonical()                       scripts/release-evidence.mjs   6520023f1b6f4ef5af6bb8ffab064a52cd755616
  signedEnvelope()/verifyEnvelope() scripts/release-dev-state.mjs  260912eb7255d7ac59e6867df94e34bc94a4c00f

This module answers one question: is this envelope a record signed by the key the
caller's policy selected? It never reads a key from an envelope or from a provider.
It does not bind a record to a pull request, release or receipt, and it does not
evaluate policy. A caller names the record kind it expects; any other kind is refused.

Signed bytes are the UTF-8 form of the App's canonical(record). Everything outside the
supported domain is refused with a stable `code`; nothing ambiguous is coerced. Where the
hub is stricter than the App, the spec lists the difference.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SCHEMA = "pineforge/dev-observation/v1"
SAFE_INTEGER = 2**53 - 1
ARRAY_INDEX_MAX = 2**32 - 2
MAX_ENVELOPE_BYTES = 1 << 20
MAX_DEPTH = 32
SIGNATURE_BYTES = 64
# RFC 8032 section 5.1: the prime order L of the Ed25519 base point.
GROUP_ORDER = 2**252 + 27742317777372353535851937790883648493
# DER SubjectPublicKeyInfo of an Ed25519 key: SEQUENCE { SEQUENCE { OID 1.3.101.112 }, BIT STRING }.
SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
SPKI_BYTES = len(SPKI_PREFIX) + 32

REJECT_CODES = (
    "ambiguous_index_key", "bad_utf8", "bom", "depth_exceeded", "duplicate_key", "envelope_shape",
    "input_type", "json_syntax", "key_encoding", "key_pin_format", "key_pin_mismatch", "key_untrusted",
    "kind_mismatch", "nonfinite_constant", "number_form", "number_range", "owner_invalid",
    "record_shape", "schema_mismatch", "signature_encoding", "signature_invalid", "signature_length",
    "signature_missing", "signature_noncanonical", "signature_type", "too_large", "unpaired_surrogate",
    "unsupported_type",
)

_INDEX_KEY = re.compile(r"0|[1-9][0-9]*")
_INT_TOKEN = re.compile(r"-?(?:0|[1-9][0-9]*)")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


class SignedRecordError(ValueError):
    """A value was refused. `code` is the stable machine-readable cause (REJECT_CODES)."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def _refuse(code, message):
    raise SignedRecordError(code, message)


def _string(text):
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        _refuse("unpaired_surrogate", "a string holds an unpaired UTF-16 surrogate")
    out = []
    for char in text:
        escaped = _SHORT_ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
        elif char < " ":
            out.append("\\u%04x" % ord(char))
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _key_order(keys):
    # The App sorts Object.keys() with the default comparator (UTF-16 code units), rebuilds the
    # object, and JSON.stringify then lists array-index keys first in ascending numeric order.
    indexed, named = [], []
    for key in keys:
        if type(key) is not str:
            _refuse("unsupported_type", "an object key is not a string")
        _string(key)
        if _INDEX_KEY.fullmatch(key) is None:
            named.append(key)
            continue
        if len(key) > 10 or int(key) > ARRAY_INDEX_MAX:
            _refuse("ambiguous_index_key", "an integer-like key is at or above 2**32 - 1")
        indexed.append((int(key), key))
    named.sort(key=lambda key: key.encode("utf-16-be"))
    return [key for _, key in sorted(indexed)] + named


def _emit(value, depth, out):
    kind = type(value)
    if value is None:
        out.append("null")
    elif kind is bool:
        out.append("true" if value else "false")
    elif kind is int:
        if not -SAFE_INTEGER <= value <= SAFE_INTEGER:
            _refuse("number_range", "an integer is outside the IEEE-754 safe range")
        out.append(str(value))
    elif kind is str:
        out.append(_string(value))
    elif kind is list:
        if depth >= MAX_DEPTH:
            _refuse("depth_exceeded", "nesting is deeper than MAX_DEPTH")
        out.append("[")
        for position, item in enumerate(value):
            if position:
                out.append(",")
            _emit(item, depth + 1, out)
        out.append("]")
    elif kind is dict:
        if depth >= MAX_DEPTH:
            _refuse("depth_exceeded", "nesting is deeper than MAX_DEPTH")
        out.append("{")
        for position, key in enumerate(_key_order(value)):
            if position:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _emit(value[key], depth + 1, out)
        out.append("}")
    else:
        _refuse("unsupported_type", "a value is not null, a bool, an integer, a string, a list or a dict")


def _text(value, depth):
    out = []
    _emit(value, depth, out)
    return "".join(out) + "\n"


def canonical_text(value):
    """The App's canonical(value) for a value inside the supported domain: compact JSON plus LF."""
    return _text(value, 0)


def canonical_bytes(value):
    """The bytes the coordinator signs: Buffer.from(canonical(value)), that is UTF-8."""
    return canonical_text(value).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _refuse("duplicate_key", "an object repeats a key")
        result[key] = value
    return result


def _integer(token):
    if _INT_TOKEN.fullmatch(token) is None:
        _refuse("json_syntax", "a number token is not a plain integer")
    if token == "-0":
        _refuse("number_form", "negative zero is not a canonical integer")
    if len(token.lstrip("-")) > 16:
        _refuse("number_range", "an integer is outside the IEEE-754 safe range")
    value = int(token)
    if not -SAFE_INTEGER <= value <= SAFE_INTEGER:
        _refuse("number_range", "an integer is outside the IEEE-754 safe range")
    return value


def _fraction_or_exponent(token):
    _refuse("number_form", "a number has a fraction or an exponent")


def _nonfinite(token):
    _refuse("nonfinite_constant", "NaN and Infinity are not JSON values")


def parse_json_strict(raw):
    """Parse UTF-8 JSON bytes into the supported domain, or refuse. Never coerces."""
    if type(raw) is not bytes:
        _refuse("input_type", "JSON input must be bytes")
    if len(raw) > MAX_ENVELOPE_BYTES:
        _refuse("too_large", "JSON input is larger than MAX_ENVELOPE_BYTES")
    if raw.startswith(b"\xef\xbb\xbf"):
        _refuse("bom", "JSON input starts with a byte order mark")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _refuse("bad_utf8", "JSON input is not strict UTF-8")
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_int=_integer,
                           parse_float=_fraction_or_exponent, parse_constant=_nonfinite)
    except SignedRecordError:
        raise
    except RecursionError:
        _refuse("depth_exceeded", "nesting is deeper than the parser allows")
    except ValueError:
        _refuse("json_syntax", "input is not valid JSON")
    canonical_text(value)
    return value


_LOADED = object()


class TrustedKey:
    """An Ed25519 public key selected and pinned by the caller's policy.

    Only load_trusted_key() builds one, and it is read-only. Verification refuses any other
    key form. This guards calling code against misuse; it is not a boundary against code
    that runs in the same process.
    """

    __slots__ = ("_public_key", "spki_sha256")

    def __init__(self, token, public_key, spki_sha256):
        if token is not _LOADED:
            raise TypeError("build a TrustedKey with load_trusted_key")
        object.__setattr__(self, "_public_key", public_key)
        object.__setattr__(self, "spki_sha256", spki_sha256)

    def __setattr__(self, name, value):
        raise AttributeError("a TrustedKey is read-only")


def load_trusted_key(spki_der, *, expected_spki_sha256):
    """Load the policy-selected key: its exact DER SubjectPublicKeyInfo bytes and the pinned digest."""
    if type(spki_der) is not bytes:
        _refuse("input_type", "the trusted key must be DER bytes")
    if type(expected_spki_sha256) is not str or _DIGEST.fullmatch(expected_spki_sha256) is None:
        _refuse("key_pin_format", "the key pin must be 64 lowercase hex characters")
    if len(spki_der) != SPKI_BYTES or not spki_der.startswith(SPKI_PREFIX):
        _refuse("key_encoding", "the trusted key is not a DER SubjectPublicKeyInfo for Ed25519")
    digest = hashlib.sha256(spki_der).hexdigest()
    if not hmac.compare_digest(digest, expected_spki_sha256):
        _refuse("key_pin_mismatch", "the trusted key does not match its pinned digest")
    return TrustedKey(_LOADED, Ed25519PublicKey.from_public_bytes(spki_der[len(SPKI_PREFIX):]), digest)


def _signature_bytes(value):
    if type(value) is not str:
        _refuse("signature_type", "the signature must be a base64 string")
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        _refuse("signature_encoding", "the signature is not canonical standard base64")
    if base64.b64encode(raw).decode("ascii") != value:
        _refuse("signature_encoding", "the signature is not canonical standard base64")
    if len(raw) != SIGNATURE_BYTES:
        _refuse("signature_length", "the signature is not 64 bytes")
    if int.from_bytes(raw[32:], "little") >= GROUP_ORDER:
        _refuse("signature_noncanonical", "the signature scalar is not below the group order")
    return raw


def _require_inputs(trusted_key, expected_kind):
    if type(trusted_key) is not TrustedKey:
        _refuse("key_untrusted", "verification needs a key loaded with load_trusted_key")
    if type(expected_kind) is not str or not expected_kind:
        _refuse("input_type", "expected_kind must be a non-empty string")


def verify_envelope(envelope, trusted_key, *, expected_kind):
    """Return the record of a parsed envelope whose signature the trusted key verifies.

    The caller guarantees that the envelope came from a duplicate-key-refusing parse;
    verify_envelope_bytes does that itself. Checks run in the App's order: the signature
    first, then schema and owner, then the caller's record kind. The envelope counts as the
    first nesting level in both entry points, so a record may nest MAX_DEPTH - 1 levels.
    """
    _require_inputs(trusted_key, expected_kind)
    if type(envelope) is not dict:
        _refuse("envelope_shape", "the envelope must be a JSON object")
    if "signature" not in envelope:
        _refuse("signature_missing", "the envelope has no signature")
    if set(envelope) != {"record", "signature"}:
        _refuse("envelope_shape", "the envelope must hold exactly record and signature")
    signature = _signature_bytes(envelope["signature"])
    record = envelope["record"]
    if type(record) is not dict:
        _refuse("record_shape", "the record must be a JSON object")
    try:
        trusted_key._public_key.verify(signature, _text(record, 1).encode("utf-8"))
    except InvalidSignature:
        _refuse("signature_invalid", "the signature does not verify under the policy key")
    if record.get("schema") != SCHEMA:
        _refuse("schema_mismatch", "the record schema is not " + SCHEMA)
    owner = record.get("owner")
    if type(owner) is not int or not 0 < owner <= SAFE_INTEGER:
        _refuse("owner_invalid", "the record owner is not a positive safe integer")
    if record.get("kind") != expected_kind:
        _refuse("kind_mismatch", "the record kind is not the kind the caller expects")
    return record


def verify_envelope_bytes(raw, trusted_key, *, expected_kind):
    """Strictly parse envelope bytes, then verify_envelope. The preferred entry point."""
    _require_inputs(trusted_key, expected_kind)
    return verify_envelope(parse_json_strict(raw), trusted_key, expected_kind=expected_kind)
