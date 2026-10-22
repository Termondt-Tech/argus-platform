"""RS256 signing for a Google service-account JWT. Standard library only.

A Google service account authenticates by signing a JWT with the RSA key in its
downloaded JSON, which is RS256: RSASSA-PKCS1-v1_5 over SHA-256. The stdlib has
SHA-256 and arbitrary-precision integers, so what is missing is the padding and the
modular exponentiation — about fifty lines, below.

**Why this exists rather than a dependency.** The runtime is stdlib-only apart from
the tastytrade SDK, and the alternative to a service account (an OAuth app) cannot
be published for personal use without a domain, a privacy policy and Google's brand
verification. → docs/decisions/2026-09-22-ledger-and-sheets.md

**What makes hand-written crypto acceptable HERE, stated so the next reader can
check the reasoning rather than the code:**

- **It only SIGNS.** It verifies nothing, decrypts nothing and parses no attacker-
  supplied data: its inputs are our own key file and our own JWT. There is no
  signature-verification bypass to get wrong, which is where the famous JWT
  vulnerabilities live.
- **A bug cannot fail quietly.** A malformed signature is rejected by Google's token
  endpoint, so the failure is a refused login, not a weakened one.
- **The timing side channel does not apply.** `pow()` is not constant-time, but
  extracting the key that way needs local measurement of this process — and anyone
  who can do that can read the key file it just opened.
- **It is pinned against a second implementation.** The tests check every signature
  against `openssl dgst -sha256 -sign`, byte for byte (PKCS#1 v1.5 is
  deterministic, so that comparison is exact), and verify with the public exponent.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

#: The DER-encoded DigestInfo prefix for SHA-256, RFC 8017 §9.2 step 2. The digest
#: is appended to it to make `T`.
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class KeyError_(ValueError):
    """The key could not be read. Named to avoid shadowing the builtin."""


@dataclass(frozen=True)
class RsaKey:
    """An RSA private key as the three integers signing needs."""

    n: int
    e: int
    d: int

    @property
    def size_bytes(self) -> int:
        """`k` in RFC 8017: the modulus length, which is the signature length."""
        return (self.n.bit_length() + 7) // 8


# -- just enough DER ----------------------------------------------------------


def _read_tlv(data: bytes, at: int) -> tuple[int, bytes, int]:
    """One DER element at `at`: (tag, contents, index after it)."""
    if at + 2 > len(data):
        raise KeyError_("truncated DER")
    tag = data[at]
    length = data[at + 1]
    at += 2
    if length & 0x80:  # long form: the low bits count the length's own bytes
        count = length & 0x7F
        if count == 0 or at + count > len(data):
            raise KeyError_("bad DER length")
        length = int.from_bytes(data[at:at + count], "big")
        at += count
    end = at + length
    if end > len(data):
        raise KeyError_("DER element runs past the end")
    return tag, data[at:end], end


def _sequence(data: bytes) -> list[tuple[int, bytes]]:
    """The elements of a DER SEQUENCE, as (tag, contents)."""
    tag, contents, _ = _read_tlv(data, 0)
    if tag != 0x30:
        raise KeyError_(f"expected a DER SEQUENCE, got tag 0x{tag:02x}")
    out: list[tuple[int, bytes]] = []
    at = 0
    while at < len(contents):
        t, v, at = _read_tlv(contents, at)
        out.append((t, v))
    return out


def _int(contents: bytes) -> int:
    return int.from_bytes(contents, "big")


def load_pem_private_key(pem: str) -> RsaKey:
    """An RSA private key from PEM. Accepts PKCS#8 (`BEGIN PRIVATE KEY`, what Google
    writes) and PKCS#1 (`BEGIN RSA PRIVATE KEY`)."""
    lines = [ln.strip() for ln in pem.strip().splitlines()]
    if not any(ln.startswith("-----BEGIN") for ln in lines):
        raise KeyError_("no PEM body found in the key (no -----BEGIN line)")
    body = [ln for ln in lines if ln and not ln.startswith("-----")]
    try:
        # `validate=True`: without it base64 SKIPS junk characters, so a corrupt key
        # decodes to garbage and fails later as a confusing DER error.
        der = base64.b64decode("".join(body), validate=True)
    except ValueError as exc:
        raise KeyError_(f"PEM body is not base64: {exc}") from exc

    elements = _sequence(der)
    if len(elements) >= 3 and elements[1][0] == 0x30 and elements[2][0] == 0x04:
        # PKCS#8: version, AlgorithmIdentifier, PrivateKey OCTET STRING wrapping
        # the PKCS#1 structure.
        elements = _sequence(elements[2][1])
    # PKCS#1 RSAPrivateKey: version, n, e, d, p, q, dp, dq, qInv.
    if len(elements) < 4 or any(t != 0x02 for t, _ in elements[:4]):
        raise KeyError_("not an RSA private key (unexpected DER shape)")
    n, e, d = (_int(elements[i][1]) for i in (1, 2, 3))
    if n <= 0 or e <= 0 or d <= 0:
        raise KeyError_("RSA key has a non-positive component")
    return RsaKey(n=n, e=e, d=d)


# -- signing ------------------------------------------------------------------


def _pkcs1_v15_pad(digest: bytes, size: int) -> bytes:
    """EM = 0x00 || 0x01 || 0xFF… || 0x00 || DigestInfo (RFC 8017 §9.2)."""
    t = _SHA256_DIGEST_INFO + digest
    if size < len(t) + 11:
        raise KeyError_(f"RSA key is too small to sign: {size * 8} bits")
    padding = b"\xff" * (size - len(t) - 3)
    return b"\x00\x01" + padding + b"\x00" + t


def sign(message: bytes, key: RsaKey) -> bytes:
    """RSASSA-PKCS1-v1_5 over SHA-256. Deterministic: one message, one signature."""
    em = _pkcs1_v15_pad(hashlib.sha256(message).digest(), key.size_bytes)
    signature = pow(int.from_bytes(em, "big"), key.d, key.n)
    return signature.to_bytes(key.size_bytes, "big")


def verify(message: bytes, signature: bytes, key: RsaKey) -> bool:
    """Check a signature with the PUBLIC exponent. Used by the tests, and by
    `sign_jwt`'s self-check; never to authenticate anything."""
    if len(signature) != key.size_bytes:
        return False
    recovered = pow(int.from_bytes(signature, "big"), key.e, key.n)
    expected = _pkcs1_v15_pad(hashlib.sha256(message).digest(), key.size_bytes)
    return recovered.to_bytes(key.size_bytes, "big") == expected


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
