"""The RS256 signer (`argus.tools.rs256`), pinned against `openssl`.

Hand-written signing earns its place only if a second implementation agrees with it,
so the central test signs with `openssl dgst -sha256 -sign` and compares bytes.
PKCS#1 v1.5 is deterministic, so that comparison is exact rather than "both verify".

Keys are generated at test time; none is committed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from argus.tools.rs256 import (
    KeyError_,
    load_pem_private_key,
    sign,
    verify,
)

openssl = shutil.which("openssl")
needs_openssl = pytest.mark.skipif(openssl is None, reason="openssl not on PATH")

MESSAGE = b"eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJhcmd1cy1sZWRnZXIifQ"


def _genkey(tmp_path: Path, bits: int = 2048, pkcs1: bool = False) -> Path:
    """A fresh RSA key in PKCS#8 (what Google ships) or PKCS#1."""
    path = tmp_path / f"key{bits}{'-pkcs1' if pkcs1 else ''}.pem"
    subprocess.run(
        [openssl, "genrsa", "-traditional", "-out", str(path), str(bits)] if pkcs1
        else [openssl, "genpkey", "-algorithm", "RSA", "-out", str(path),
              "-pkeyopt", f"rsa_keygen_bits:{bits}"],
        check=True, capture_output=True,
    )
    return path


def _openssl_sign(key: Path, message: bytes, tmp_path: Path) -> bytes:
    msg = tmp_path / "msg.bin"
    msg.write_bytes(message)
    out = subprocess.run(
        [openssl, "dgst", "-sha256", "-sign", str(key), str(msg)],
        check=True, capture_output=True,
    )
    return out.stdout


@needs_openssl
def test_the_signature_is_byte_for_byte_what_openssl_produces(tmp_path):
    key_path = _genkey(tmp_path)
    key = load_pem_private_key(key_path.read_text())

    assert sign(MESSAGE, key) == _openssl_sign(key_path, MESSAGE, tmp_path)


@needs_openssl
@pytest.mark.parametrize("bits", [2048, 3072])
def test_it_agrees_with_openssl_at_other_key_sizes(tmp_path, bits):
    key_path = _genkey(tmp_path, bits)
    key = load_pem_private_key(key_path.read_text())

    assert len(sign(MESSAGE, key)) == bits // 8
    assert sign(MESSAGE, key) == _openssl_sign(key_path, MESSAGE, tmp_path)


@needs_openssl
def test_a_pkcs1_key_reads_too(tmp_path):
    """Google writes PKCS#8; PKCS#1 is what older tooling produces."""
    key_path = _genkey(tmp_path, pkcs1=True)
    key = load_pem_private_key(key_path.read_text())

    assert sign(MESSAGE, key) == _openssl_sign(key_path, MESSAGE, tmp_path)


@needs_openssl
def test_openssl_verifies_our_signature_with_the_public_key(tmp_path):
    key_path = _genkey(tmp_path)
    pub = tmp_path / "pub.pem"
    subprocess.run([openssl, "rsa", "-in", str(key_path), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True)
    sig = tmp_path / "sig.bin"
    sig.write_bytes(sign(MESSAGE, load_pem_private_key(key_path.read_text())))
    msg = tmp_path / "msg.bin"
    msg.write_bytes(MESSAGE)

    subprocess.run(
        [openssl, "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig), str(msg)],
        check=True, capture_output=True,
    )


@needs_openssl
def test_a_signature_is_specific_to_its_message_and_its_key(tmp_path):
    key_a = load_pem_private_key(_genkey(tmp_path).read_text())
    other = tmp_path / "other"
    other.mkdir()
    key_b = load_pem_private_key(_genkey(other).read_text())

    signature = sign(MESSAGE, key_a)
    assert verify(MESSAGE, signature, key_a)
    assert not verify(MESSAGE + b"x", signature, key_a), "a changed message fails"
    assert not verify(MESSAGE, signature, key_b), "another key's signature fails"
    assert not verify(MESSAGE, signature[:-1], key_a), "a truncated signature fails"


# -- refusals -----------------------------------------------------------------


def test_junk_is_refused_rather_than_signed():
    with pytest.raises(KeyError_, match="no PEM body"):
        load_pem_private_key("not a key at all")
    with pytest.raises(KeyError_, match="base64"):
        load_pem_private_key(
            "-----BEGIN PRIVATE KEY-----\n!!!!\n-----END PRIVATE KEY-----"
        )
    with pytest.raises(KeyError_, match="DER|RSA"):
        load_pem_private_key("-----BEGIN PRIVATE KEY-----\nZm9v\n-----END PRIVATE KEY-----")


@needs_openssl
def test_an_ec_key_is_refused_not_misread(tmp_path):
    """RS256 is RSA. An EC key must fail here, not produce a wrong signature."""
    path = tmp_path / "ec.pem"
    subprocess.run(
        [openssl, "genpkey", "-algorithm", "EC", "-pkeyopt",
         "ec_paramgen_curve:P-256", "-out", str(path)],
        check=True, capture_output=True,
    )
    with pytest.raises(KeyError_):
        load_pem_private_key(path.read_text())
