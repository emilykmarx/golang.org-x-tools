#!/usr/bin/env python3
"""Contents of files/IDs a concrete config refers to (TLS certs and keys, private keys, UUIDs).
Contents are deterministic given the same seed.
Import it, or run it to print a file.

Usage:
  uv run --with pycryptodome --with cryptography python files.py cert <seed>   # a self-signed CA cert, then its key
  uv run --with pycryptodome --with cryptography python files.py key <seed>    # an RSA private key (PKCS#8 PEM)
  uv run --with pycryptodome --with cryptography python files.py uuid <seed>
"""

import datetime
import hashlib
import sys
import uuid

from Crypto.PublicKey import RSA
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID


def rsa_key(seed):
    """A throwaway 2048-bit RSA key (pycryptodome), the same for the same seed: RSA.generate with its randomness
    drawn from SHA-256 in counter mode over the seed"""
    buf, n = b"", 0

    def randfunc(k):
        nonlocal buf, n
        while len(buf) < k:
            buf += hashlib.sha256(f"{seed}:{n}".encode()).digest()
            n += 1
        out, buf = buf[:k], buf[k:]
        return out

    return RSA.generate(2048, randfunc=randfunc)


def rsa_key_pem(seed):
    """rsa_key as PKCS#8 PEM, e.g. for a service account's private_key"""
    return rsa_key(seed).export_key(format="PEM", pkcs=8).decode()


def cert(seed):
    """A throwaway self-signed CA cert and its key, as PEM strings: the same for the same seed (RSA PKCS#1 v1.5
    signatures, fixed fields; the CN is the seed, or its SHA-256 if longer than 64 characters). Since it's self-signed, the cert is also its own CA, e.g. for a tls_config's ca_file,
    cert_file and key_file."""
    key = serialization.load_pem_private_key(rsa_key(seed).export_key(format="PEM", pkcs=8), password=None)
    # A CN is at most 64 characters, so a longer seed (e.g. a value.py token) is hashed
    cn = seed if len(seed) <= 64 else hashlib.sha256(seed.encode()).hexdigest()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
         .serial_number(1).not_valid_before(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
         .not_valid_after(datetime.datetime(2036, 1, 1, tzinfo=datetime.timezone.utc))
         .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
         .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return c.public_bytes(serialization.Encoding.PEM).decode(), key_pem.decode()


def uuid_from(seed):
    """A UUID derived from the seed, for fields that must be UUIDs"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("cert", "key", "uuid"):
        sys.exit(__doc__)
    kind, seed = sys.argv[1:]
    if kind == "cert":
        print("".join(cert(seed)), end="")
    elif kind == "key":
        print(rsa_key_pem(seed))
    else:
        print(uuid_from(seed))


if __name__ == "__main__":
    main()
