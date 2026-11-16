"""Makes the kube-apiserver fixtures' keys (fixtures.py), TEST-ONLY: ca.crt/ca.key (the CA the concrete configs name:
the authentication config's certificateAuthority, the authz webhook's kubeconfig), server.crt/server.key (the
fixtures' TLS certificate, for HOSTS' names) and jwt.key (the OIDC issuer's signing key). Fixed, and committed, since
the concrete configs embed the CA; rerun only to replace them (then update the configs).
Usage: uv run --with cryptography python make_keys.py"""

import datetime
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = ["ccfg-issuer.example.com", "ccfg-discovery.example.com", "ccfg-authz.example.com", "ccfg-audit.example.com",
         "ccfg-otlp-collector.example.com"]


def pem_key(k):
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def main():
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    end = now + datetime.timedelta(days=3650)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ccfg-fixtures-ca")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(end)
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    server = (x509.CertificateBuilder()
              .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NAMES[0])])).issuer_name(ca_name)
              .public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now).not_valid_after(end)
              .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in NAMES]), critical=False)
              .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
              .sign(ca_key, hashes.SHA256()))
    files = {"ca.crt": ca.public_bytes(serialization.Encoding.PEM), "ca.key": pem_key(ca_key),
             "server.crt": server.public_bytes(serialization.Encoding.PEM), "server.key": pem_key(key),
             "jwt.key": pem_key(rsa.generate_private_key(public_exponent=65537, key_size=2048))}
    for name, data in files.items():
        open(f"{HERE}/{name}", "wb").write(data)
    print("wrote " + ", ".join(files))


if __name__ == "__main__":
    main()
