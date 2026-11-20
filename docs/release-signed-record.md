# Application signed records: canonical form and verification

Evidence label for every vector in this repository: **test-key vectors, not a live receipt proof**.
Nothing here proves that a live receipt was signed. The later real-key, proof-kind vector is a separate gap.

This document is the written contract for `scripts/release_signed_record.py`, which decides whether one
envelope written by the App's dev coordinator is a genuine record signed by the key the hub's policy
selected. It does not bind a record to a pull request, a release or a receipt, it holds no policy values
and it changes no workflow. Those belong to the receipt validator that calls it.

## Pinned sources

The canonical form is derived from these App sources, identified by role and git blob id. A change to any of these blobs
needs a new review of this document, the vectors and the module together.

| App source role | git blob | What the hub takes from it |
|---|---|---|
| Canonical encoder | `6520023f1b6f4ef5af6bb8ffab064a52cd755616` | `canonical(value)` |
| Signed record state | `260912eb7255d7ac59e6867df94e34bc94a4c00f` | `signedEnvelope()` and `verifyEnvelope()` |
| App release coordinator | `0c96484de8e788bc8eceef858e3cc9248e917fe4` | the record kinds the coordinator writes through `publishRecord()` |

The strict JSON parser, release readiness helpers and the coordinator's Node version are not pinned.
The vectors were written against
`JSON.stringify` as specified from ECMAScript 2019 on (well-formed output for lone surrogates).

## What is signed

The App signs `Buffer.from(canonical(record))`, which is the UTF-8 encoding of the string
`JSON.stringify(sorted(record)) + "\n"`, with pure Ed25519 (RFC 8032, no prehash, no context):
`sign(null, bytes, privateKey)`. The envelope is exactly `{"record": <object>, "signature": "<base64>"}`,
where the signature is the standard base64 of the 64 signature bytes. A record is a JSON object that
the coordinator built as `{...value, schema: "pineforge/dev-observation/v1", owner, owner_sha}`. Its
`kind` names what it is: `lease`, `lease-aborted`, `begin`, `rollback-point`, `snapshot`, `post-merge`,
`post-merge-finished`, `complete`, `good`, `settled` or `receipt-run`.

## Canonical form

`canonical(value)` applies, in this order:

1. **Recursive order.** Every object, at every depth and inside arrays, is rebuilt with its keys in a fixed
   order. Array elements keep their order.
2. **Key order is JavaScript's, not alphabetical.** The App sorts `Object.keys()` with the default
   comparator, which compares UTF-16 code units, and rebuilds the object. `JSON.stringify` then lists
   own keys in the order ECMAScript defines: keys that are array indexes first, in ascending numeric
   order, then every other key in creation order, which is the sorted order. An array index is a
   canonical decimal integer from `0` to `4294967294` (2**32 - 2). So `{"b":1,"10":2,"2":3}` is
   written `{"2":3,"10":2,"b":1}`. `01`, `-1`, `1.5` and ` 1` are ordinary keys. UTF-16 order is not
   code point order: U+1F600 (units D83D DE00) sorts before U+E000. Keys sort on their characters,
   not on their escaped spelling. `__proto__` is an ordinary key.
3. **Separators.** No whitespace anywhere: `,` between members, `:` between key and value.
4. **Strings and keys.** Quote, backslash, backspace, form feed, LF, CR and tab are written `\"`, `\\`,
   `\b`, `\f`, `\n`, `\r`, `\t`. Every other code point below U+0020 is written `\u00xx` with lowercase
   hex. Everything else is written raw, including U+007F, U+2028, U+2029, `/` and all non-ASCII text.
   There is no Unicode normalization: precomposed U+00E9 and `e` plus U+0301 are different strings.
5. **Numbers.** The hub signs only integers from -(2**53 - 1) to 2**53 - 1, written in plain decimal
   with no sign other than a leading minus, no leading zeros, no fraction and no exponent.
6. **Literals.** `null`, `true` and `false`. `null` is a value and is signed. An absent key is not null:
   they sign different bytes. A signer-side `undefined` also vanishes from objects, but JSON text cannot
   carry `undefined`, so the hub never sees one.
7. **End.** One LF after the value. The signed bytes are the UTF-8 encoding of that text.

## Supported domain and reject boundaries

A value outside the domain is refused with a stable code. Nothing is coerced, rounded, normalized or
dropped. `SignedRecordError.code` carries one of these:

| Code | What is refused | What the App does with it |
|---|---|---|
| `input_type` | a non-bytes input, a non-bytes key, an `expected_kind` that is not a non-empty string (leaving the keyword out is a Python `TypeError`) | n/a |
| `too_large` | more than 1 MiB of JSON | no bound |
| `bom` | JSON starting with a byte order mark | n/a |
| `bad_utf8` | bytes that are not strict UTF-8 | n/a |
| `json_syntax` | anything `json` cannot parse: trailing data, comments, trailing commas, single quotes, leading zeros, raw control characters | `JSON.parse` throws |
| `nonfinite_constant` | `NaN`, `Infinity`, `-Infinity` | `JSON.parse` throws |
| `duplicate_key` | a repeated key, also after unescaping | the last duplicate silently wins |
| `number_form` | any number with a fraction or exponent, and `-0` (`1.0`, `1e2`, `1.5`) | `1.0` signs as `1`, `1e2` as `100`, `-0` as `0`, `1e999` as `null` |
| `number_range` | an integer beyond 2**53 - 1 in magnitude | `9007199254740993` is rounded to `9007199254740992` |
| `unsupported_type` | a Python value that is not None, bool, int, str, list or dict; a dict or str subclass; a non-string key | n/a |
| `unpaired_surrogate` | a string or key holding an unpaired UTF-16 surrogate | written as the six characters `\udxxx` |
| `ambiguous_index_key` | an integer-like key of 2**32 - 1 or more | engine-defined order |
| `depth_exceeded` | nesting deeper than 32 levels, counting the envelope as the first level so a record may nest 31, or deeper than the parser can read | no bound |

A well-formed surrogate pair written as two `\u` escapes in JSON is one code point and is accepted.

An input with more than one defect is refused with the code of the first defect the parser reaches.
For example `{"a":-01}` is `number_form`, because the parser sees the token `-0` before it sees the
stray `1`. Each defect alone has the code in the table.

## Verification

`verify_envelope_bytes(raw, trusted_key, expected_kind=...)` refuses at the first failing step:

1. `trusted_key` must come from `load_trusted_key` (`key_untrusted`) and `expected_kind` must be a
   non-empty string (`input_type`).
2. Parse strictly: the reject boundaries above.
3. The envelope must be an object: `envelope_shape`. With no `signature` key it is `signature_missing`.
   With anything other than exactly `record` and `signature` it is `envelope_shape`. A `public_key`, a `kid`
   or any other extra field in an envelope is therefore refused, never read.
4. The signature must be a string (`signature_type`), canonical padded standard base64
   (`signature_encoding`) of exactly 64 bytes (`signature_length`) whose scalar S is below the group
   order L (`signature_noncanonical`).
5. The record must be an object: `record_shape`.
6. Verify the signature over `canonical_bytes(record)` with the trusted key only: `signature_invalid`.
7. Only then, as the App does, the record must carry `schema` equal to `pineforge/dev-observation/v1`
   (`schema_mismatch`), an `owner` that is a positive safe integer (`owner_invalid`) and a `kind` equal to
   the kind the caller asked for (`kind_mismatch`).

`verify_envelope` runs steps 3 to 7 on an already parsed envelope; the caller guarantees the parse
refused duplicate keys. Prefer the bytes entry point.

Kind is the caller's choice, not the envelope's. `verifyEnvelope` in the App never reads `kind`; each
App caller checks it afterwards. The hub makes that check part of verification so a correctly signed
`snapshot` record can never stand in for a `receipt-run` binder or the reverse.

## Trusted key

The key is chosen by hub policy and arrives as the exact DER SubjectPublicKeyInfo bytes of an Ed25519
key together with the SHA-256 the policy pins for them. The live policy pin is
`520945039e0d2d4b3e28f9e9fb87045af79c4358479677effad6b427ea7c7b71`.
`load_trusted_key(spki_der, expected_spki_sha256=...)` accepts only the 44-byte form
`302a300506032b6570032100` plus 32 key bytes (`key_encoding`), a pin of 64 lowercase hex characters
(`key_pin_format`) and a key whose digest equals the pin (`key_pin_mismatch`). The module never
embeds, selects or reads a key. It does not look at an envelope field, a record field or any provider
field for key material. Test keys can never satisfy the live pin. A `TrustedKey` can only be built by
`load_trusted_key` and is read-only; that guards calling code against misuse and is not a boundary
against code running in the same process.

## Where the hub is stricter than the App

| Input | App | Hub | Pinned by |
|---|---|---|---|
| number spelled `1.0`, `1e2`, `-0` | accepted, signs the integer value | `number_form` | vectors and fixtures |
| number with a fraction, `1.5` | accepted, signs `1.5` | `number_form` | vector `number-fraction` |
| integer beyond 2**53 - 1 | accepted, may round | `number_range` | vectors |
| duplicate keys | last wins | `duplicate_key` | vectors |
| unpaired surrogate | accepted | `unpaired_surrogate` | vectors |
| integer-like key of 2**32 - 1 or more | accepted | `ambiguous_index_key` | vector |
| extra envelope fields | ignored | `envelope_shape` | fixtures |
| signature without padding, or with trailing whitespace | accepted by Node's lenient decoder | `signature_encoding` | fixtures |
| signature in the URL-safe alphabet, or with non-zero trailing bits | accepted by Node's lenient decoder | `signature_encoding` | unit test only |
| signature scalar S + L | decided by the OpenSSL build | `signature_noncanonical` | fixture, App not pinned |
| record kind | never checked by `verifyEnvelope` | must equal the caller's `expected_kind` | fixtures |
| record that is not an object | fails at the schema check | `record_shape` | fixture, both refuse |
| nesting depth and size | none | 32 levels, 1 MiB | unit tests only |

Rows pinned by vectors or fixtures record both decisions in the shared files. Rows marked unit test
have no App counterpart to record or no shared vector.

## Vector files

Both files live in `tests/fixtures/signed-record/`, are ASCII, and carry the label above.

- `canonical-vectors.json` holds the canonical-byte vectors. Each has the exact input text, the App's
  result (`canonical` with its text, or `throws`) and the hub's decision. Where the hub accepts, the
  bytes are pinned as text and as UTF-8 hex. No key, signature or receipt is involved. These
  vectors were derived from the pinned `canonical()` source by reading it, then are checked against the
  App's own function on the approved Spot box.
- `signature-vectors.json` holds the accepted test-key envelope and the signature-negative fixtures.
  Test keys are derived from two public seed phrases (seed = SHA-256 of the phrase) and no policy
  trusts them. The signed bytes are generated on an approved Spot box by the App's own
  `signedEnvelope()` text; until then they are pending and the pre-signed tests fail by name.

The two kinds are kept apart on purpose. A canonical-byte vector says what bytes a value signs. A
signature fixture says what a verifier does with a signature over those bytes. Each fixture's `mutates`
field says what changed:

- `record-representation`: the signed value is unchanged (key order, Unicode escaping, whitespace,
  number spelling). The hub accepts it or refuses the spelling; it must never answer `signature_invalid`.
- `record-value`: the signed value changed (one byte, a number, `null` added or removed, an accent). The
  answer is always `signature_invalid`.
- `signature`, `key`, `envelope` and `signed-semantics`: the defect is in the signature, the key, the
  envelope shape, or a correctly signed record the caller must still refuse.

The App pins the same two files byte for byte and runs `canonical()` and `verifyEnvelope()` over them.
Divergence between the App and hub results is recorded in the vectors, not hidden.

## Module interface

```python
SignedRecordError(ValueError)                 # .code is one of REJECT_CODES
parse_json_strict(raw: bytes) -> value        # strict parse into the domain, or refuse
canonical_text(value) -> str                  # the App's canonical(value)
canonical_bytes(value) -> bytes               # UTF-8 of canonical_text(value)
load_trusted_key(spki_der: bytes, *, expected_spki_sha256: str) -> TrustedKey
verify_envelope_bytes(raw: bytes, trusted_key, *, expected_kind: str) -> dict
verify_envelope(envelope: dict, trusted_key, *, expected_kind: str) -> dict
```

Both verify functions return the verified record. The Ed25519 primitive is `cryptography`'s
`Ed25519PublicKey`; this module implements no cryptography of its own.

## Not covered

Receipt binding, the policy that holds the pinned key, workflow changes, a real-key signed proof-kind
vector and any claim that a live receipt verified are outside this document.
