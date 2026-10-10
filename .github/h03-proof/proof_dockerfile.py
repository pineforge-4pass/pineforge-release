"""Write docker/Dockerfile.h03proof: the hub's docker/Dockerfile with ONLY its two download stages
(codegen from PyPI, engine from a release tarball) replaced by stubs that take the engine's files
from the build context (engine-install/). Everything before Stage 1 and from Stage 3 on, which holds
the harness COPY lines, the harness sha256 check and the ABI check, is carried over byte for byte;
this script asserts that and prints the diff against the real Dockerfile."""
import difflib
import sys
from pathlib import Path

REAL = Path("docker/Dockerfile")
PROOF = Path("docker/Dockerfile.h03proof")
START = "# --- Stage 1:"
FINAL = "# --- Stage 3:"

STUBS = """# --- PROOF STUB (h03-proof): the codegen stage, no PyPI download ---
FROM debian:bookworm-slim AS codegen
RUN mkdir -p /opt/codegen && echo "h03 proof stub: no transpiler" > /opt/codegen/PROOF-STUB

# --- PROOF STUB (h03-proof): the engine stage, lib and headers come from the build context ---
FROM debian:bookworm-slim AS engine
COPY engine-install/ /opt/pineforge/

"""

text = REAL.read_text(encoding="utf-8")
if text.count(START) != 1 or text.count(FINAL) != 1:
    sys.exit("ERROR: the Dockerfile no longer has exactly one Stage 1 and one Stage 3 marker")
head, rest = text.split(START)
_, tail = rest.split(FINAL)
proof = head + STUBS + FINAL + tail
if not proof.startswith(head) or not proof.endswith(FINAL + tail):
    sys.exit("ERROR: the proof Dockerfile does not carry the real head and tail unchanged")
PROOF.write_text(proof, encoding="utf-8")
print("".join(difflib.unified_diff(text.splitlines(True), proof.splitlines(True), "docker/Dockerfile", str(PROOF), n=0)))
print(f"head ({len(head.splitlines())} lines) and tail ({len((FINAL + tail).splitlines())} lines) are the real Dockerfile's, unchanged")
