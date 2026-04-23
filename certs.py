"""
Certificate management: CA, server, operator, and client cert generation.
Also provides the mTLS request handler that injects peer cert DER into WSGI environ.
"""

import datetime
import hashlib
import ipaddress
import logging
import os
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from werkzeug.serving import WSGIRequestHandler

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cert paths
# ---------------------------------------------------------------------------
CERTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
CA_CERT_PATH = os.path.join(CERTS_DIR, "ca.crt")
CA_KEY_PATH = os.path.join(CERTS_DIR, "ca.key")
SERVER_CERT_PATH = os.path.join(CERTS_DIR, "server.crt")
SERVER_KEY_PATH = os.path.join(CERTS_DIR, "server.key")
OPERATOR_CERT_PATH = os.path.join(CERTS_DIR, "operator.crt")
OPERATOR_KEY_PATH = os.path.join(CERTS_DIR, "operator.key")


def _pem_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def ensure_ca_exists():
    """Load or create the CA cert/key pair. Returns (ca_cert, ca_key)."""
    if os.path.exists(CA_CERT_PATH) and os.path.exists(CA_KEY_PATH):
        with open(CA_KEY_PATH, "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), password=None)
        with open(CA_CERT_PATH, "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read())
        log.info("Loaded existing CA from %s", CA_CERT_PATH)
        return ca_cert, ca_key

    log.info("Generating new CA cert/key...")
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RMM-CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650)
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    _pem_write(
        CA_KEY_PATH,
        ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    _pem_write(CA_CERT_PATH, ca_cert.public_bytes(serialization.Encoding.PEM))
    log.info("CA written to %s", CERTS_DIR)
    return ca_cert, ca_key


def _parse_server_sans():
    """
    Build the SAN list for the server cert from environment variables.

    SERVER_IPS   — comma-separated extra IPv4/IPv6 addresses (e.g. "192.168.1.50,10.0.0.1")
    SERVER_HOSTS — comma-separated extra DNS names      (e.g. "rmm.example.com,rmm.lan")

    127.0.0.1 and localhost are always included.
    """
    sans = [
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.DNSName("localhost"),
    ]
    for raw in os.environ.get("SERVER_IPS", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(raw)))
            log.info("Server cert SAN: IP %s", raw)
        except ValueError:
            log.warning("SERVER_IPS: skipping invalid address %r", raw)
    for raw in os.environ.get("SERVER_HOSTS", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        sans.append(x509.DNSName(raw))
        log.info("Server cert SAN: DNS %s", raw)
    return sans


def ensure_server_cert_exists(ca_cert, ca_key):
    """Create the server cert signed by our CA if it doesn't already exist.

    To add new IPs/hostnames, delete certs/server.crt and certs/server.key,
    set SERVER_IPS and/or SERVER_HOSTS, then restart — the cert will regenerate.
    """
    if os.path.exists(SERVER_CERT_PATH) and os.path.exists(SERVER_KEY_PATH):
        return

    sans = _parse_server_sans()
    log.info("Generating server cert/key with %d SAN(s)...", len(sans))
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rmm-server")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    _pem_write(
        SERVER_KEY_PATH,
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    _pem_write(SERVER_CERT_PATH, cert.public_bytes(serialization.Encoding.PEM))
    log.info("Server cert written to %s", CERTS_DIR)


def ensure_operator_cert_exists(ca_cert, ca_key):
    """Create a long-lived operator cert for the management TUI if absent."""
    if os.path.exists(OPERATOR_CERT_PATH) and os.path.exists(OPERATOR_KEY_PATH):
        return

    log.info("Generating operator cert/key for TUI...")
    op_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rmm-operator")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(op_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(op_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    _pem_write(
        OPERATOR_KEY_PATH,
        op_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    _pem_write(OPERATOR_CERT_PATH, cert.public_bytes(serialization.Encoding.PEM))
    log.info(
        "Operator cert written to %s and %s", OPERATOR_CERT_PATH, OPERATOR_KEY_PATH
    )


def generate_client_cert(agent_id, ca_cert, ca_key):
    """Generate a client cert with CN=agent_id signed by the CA."""
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, agent_id)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(client_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return cert, client_key


def cert_fingerprint(cert_der: bytes) -> str:
    """SHA-256 fingerprint of a DER-encoded certificate."""
    return hashlib.sha256(cert_der).hexdigest()


# ---------------------------------------------------------------------------
# Custom request handler — injects the peer cert DER into the WSGI environ
# ---------------------------------------------------------------------------


class MTLSRequestHandler(WSGIRequestHandler):
    def make_environ(self):
        environ = super().make_environ()
        if isinstance(self.connection, ssl.SSLSocket):
            peer_der = self.connection.getpeercert(binary_form=True)
            if peer_der:
                environ["SSL_CLIENT_CERT_DER"] = peer_der
        return environ
