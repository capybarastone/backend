"""
Backend flask app for handling endpoint check-ins and task results.
"""

# standard library
import datetime
import hashlib
import ipaddress
import logging
import os
import ssl
import threading
import time
import uuid

# pypi
import toml
from dotenv import load_dotenv

load_dotenv()
from apiflask import APIFlask, Schema
from apiflask.fields import String, Integer, Boolean, Dict, Raw
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from flask import Flask as PlainFlask
from flask import jsonify, request
from marshmallow import INCLUDE
from werkzeug.serving import WSGIRequestHandler, make_server

from database import EndpointDatabase, generate_endpoint_id
from util import get_current_timestamp, primitive_log

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

# Cron config
CRON_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "cron.toml"
)
CRON_MIN_INTERVAL = 5
CRON_DEFAULT_INTERVAL = 30

_cron_state = {"interval": CRON_DEFAULT_INTERVAL, "page_refresh_interval": 10}
_cron_lock = threading.Lock()

# Populated at startup by ensure_ca_exists()
_ca_cert = None
_ca_key = None

# ---------------------------------------------------------------------------
# Cert utilities
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Cron helpers
# ---------------------------------------------------------------------------


def _load_cron_config():
    """Return the persisted cron config as a dict, falling back to defaults."""
    defaults = {
        "inventory_interval": CRON_DEFAULT_INTERVAL,
        "page_refresh_interval": 10,
    }
    if os.path.exists(CRON_CONFIG_PATH):
        try:
            data = toml.load(CRON_CONFIG_PATH)
            return {**defaults, **{k: int(v) for k, v in data.items() if k in defaults}}
        except Exception:
            pass
    return defaults


def _save_cron_config(interval, page_refresh_interval):
    """Persist the cron config to disk."""
    os.makedirs(os.path.dirname(CRON_CONFIG_PATH), exist_ok=True)
    with open(CRON_CONFIG_PATH, "w") as f:
        f.write(
            toml.dumps(
                {
                    "inventory_interval": interval,
                    "page_refresh_interval": page_refresh_interval,
                }
            )
        )


def _cron_worker():
    """
    Background thread: dispatches inventory tasks to all active (non-blacklisted)
    agents on a configurable interval.  Skips agents that already have a pending
    (not yet responded) inventory task so we never pile them up.
    """
    while True:
        with _cron_lock:
            interval = _cron_state["interval"]

        try:
            for agent_id in db.list_endpoints():
                data = db.get_endpoint(agent_id)
                if not data or data.get("blacklisted"):
                    continue
                has_pending = any(
                    t.get("instruction") == "inventory" and not t.get("responded")
                    for t in data.get("tasks", [])
                )
                if not has_pending:
                    task = {
                        "task_id": str(uuid.uuid4()),
                        "assigned_at": get_current_timestamp(),
                        "instruction": "inventory",
                        "arg": None,
                    }
                    db.add_task(agent_id, task)
                    log.debug("Cron: queued inventory for agent %s", agent_id)
        except Exception:
            log.exception("Cron worker encountered an error")

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Main app (mTLS, port 8443)
# ---------------------------------------------------------------------------

app = APIFlask(__name__)
db = EndpointDatabase()


@app.before_request
def verify_client_cert():
    """Enforce cert-pinning on all agent-facing endpoints."""
    if not request.path.startswith("/api/end/"):
        return

    cert_der = request.environ.get("SSL_CLIENT_CERT_DER")
    if not cert_der:
        return jsonify({"error": "client certificate required"}), 401

    try:
        cert = x509.load_der_x509_certificate(cert_der)
        cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if not cn_attrs:
            return jsonify({"error": "certificate missing CN"}), 401
        agent_id = cn_attrs[0].value
    except Exception:
        return jsonify({"error": "invalid certificate"}), 401

    if db.is_blacklisted(agent_id):
        return jsonify({"error": "agent blacklisted"}), 403

    stored_fp = db.get_cert_fingerprint(agent_id)
    if stored_fp is None:
        return jsonify({"error": "agent not enrolled"}), 401

    presented_fp = cert_fingerprint(cert_der)
    if presented_fp != stored_fp:
        db.blacklist_endpoint(agent_id)
        log.warning("Cert mismatch for agent %s — blacklisted", agent_id)
        return jsonify({"error": "certificate mismatch — agent blacklisted"}), 403


class CheckinQuerySchema(Schema):
    agentid = String(
        required=True,
        metadata={"description": "Unique identifier for the endpoint."},
    )


class TaskSchema(Schema):
    class Meta:
        unknown = INCLUDE

    task_id = String(
        required=True,
        metadata={"description": "Unique identifier for the task."},
    )
    assigned_at = String(
        metadata={"description": "Timestamp when the task was assigned."}
    )
    instruction = String(
        metadata={"description": "Instruction that the endpoint should execute."}
    )
    arg = String(
        allow_none=True,
        metadata={"description": "Argument associated with the instruction."},
    )
    exit_code = Integer(
        allow_none=True,
        metadata={"description": "Exit code returned by the task execution."},
    )
    stdout = String(metadata={"description": "Captured standard output."})
    stderr = String(metadata={"description": "Captured standard error."})
    stopped_processing_at = String(
        metadata={"description": "Timestamp when the task finished processing."}
    )
    responded = Boolean(
        metadata={
            "description": "Whether the endpoint has already responded to the task."
        }
    )
    inventory = Dict(
        keys=String(),
        values=Raw(),
        allow_none=True,
        metadata={"description": "Inventory payload returned by inventory tasks."},
    )


class PostResultSchema(TaskSchema):
    agent_id = String(
        required=True,
        metadata={"description": "Agent identifier reporting the result."},
    )

    class Meta:
        unknown = INCLUDE


class RegisterPayloadSchema(Schema):
    agent_id = String(dump_only=True)

    class Meta:
        unknown = INCLUDE


class RegisterResponseSchema(Schema):
    agent_id = String(
        required=True,
        metadata={"description": "Unique identifier assigned to the endpoint."},
    )


class PostTaskSchema(Schema):
    agentid = String(
        required=True,
        metadata={"description": "Agent identifier to queue the task for."},
    )
    task = Dict(
        required=True,
        keys=String(),
        values=Raw(),
        metadata={"description": "Task payload to queue."},
    )

    class Meta:
        unknown = INCLUDE


class StatusSchema(Schema):
    status = String(metadata={"description": "Human-readable status message."})


# START ENDPOINT ROUTES
@app.post("/api/end/checkin")
@app.input(CheckinQuerySchema, location="query")
@app.output(
    TaskSchema(many=True), status_code=200, description="Queued tasks for the endpoint."
)
def checkin(query_data):
    """Check in an endpoint and retrieve any queued tasks."""
    agentid = query_data["agentid"]
    endpoint = db.get_endpoint(agentid)

    if endpoint is None:
        return "unknown agentid", 404

    last_seen = get_current_timestamp()
    endpoint["last_seen"] = last_seen
    db.save_endpoint(agentid, endpoint)

    tasks = db.get_tasks_for_endpoint(agentid)
    if tasks:
        return tasks
    return "no tasks", 204


@app.post("/api/end/post_result")
@app.input(PostResultSchema)
@app.output(
    StatusSchema,
    status_code=200,
    description="Confirmation that the result was stored.",
)
def post_result(json_data):
    """Submit the outcome of a task that has been executed by an endpoint."""
    data = dict(json_data)
    agentid = data.pop("agent_id", None) or data.pop("agentid", None)
    if not agentid:
        primitive_log(
            "FLASK - Post Results",
            "Missing agent identifier in post_result payload. Content: " + str(data),
        )
        return "missing parameters", 400

    task_id = data.get("task_id") or data.get("id")
    if not task_id:
        primitive_log(
            "FLASK - Post Results",
            "Missing task identifier in post_result payload. Content: " + str(data),
        )
        return "missing parameters", 400

    success = db.post_task_result(agentid, data)
    if not success:
        return "failed to post result", 400

    return {"status": "result posted"}, 200


@app.post("/api/end/register")
@app.input(RegisterPayloadSchema, location="json")
@app.output(
    RegisterResponseSchema, status_code=200, description="Registration response."
)
def register_endpoint(json_data):
    """Register a new endpoint and obtain its assigned agent identifier."""
    remote_addr = request.remote_addr
    now = get_current_timestamp()
    payload = dict(json_data)

    app.logger.info(
        "Incoming %s request to %s from %s with payload %s",
        request.method,
        request.path,
        remote_addr,
        payload,
    )

    hostname = payload.get("hostname")
    agent_id = None

    if hostname:
        for existing_id in db.list_endpoints():
            existing_data = db.get_endpoint(existing_id)
            if not existing_data:
                continue
            if (
                existing_data.get("hostname") == hostname
                and existing_data.get("ip_address") == remote_addr
            ):
                agent_id = existing_id
                existing_data.update(
                    {key: value for key, value in payload.items() if key != "tasks"}
                )
                existing_data["ip_address"] = remote_addr
                existing_data["last_seen"] = now
                existing_data.setdefault("registered_at", now)
                db.save_endpoint(existing_id, existing_data)
                break

    if agent_id:
        return {"agent_id": agent_id}, 200

    agent_id = generate_endpoint_id()
    payload["ip_address"] = remote_addr
    payload["registered_at"] = now
    payload["last_seen"] = now

    if not db.register_endpoint(agent_id, payload):
        return "failed to register endpoint. probably a duplicate?", 400

    return {"agent_id": agent_id}, 200


# END ENDPOINT ROUTES


# START MANAGEMENT ROUTES
@app.post("/api/man/post_task")
@app.input(PostTaskSchema)
@app.output(StatusSchema, status_code=200, description="Task acceptance confirmation.")
def post_task(json_data):
    """Create a task for the given agent."""
    agent_id = json_data["agentid"]
    task_payload = json_data["task"]

    endpoint = db.get_endpoint(agent_id)
    if endpoint is None:
        return "unknown agentid", 404

    res = db.add_task(agent_id, task_payload)
    if not res:
        return "failed to add task", 400
    return {"status": "success"}, 200


class CronConfigSchema(Schema):
    inventory_interval = Integer(
        required=True,
        metadata={
            "description": "Seconds between automatic inventory tasks per agent."
        },
    )
    page_refresh_interval = Integer(
        load_default=0,
        metadata={
            "description": "Seconds between automatic page refreshes in the UI. 0 = disabled."
        },
    )


@app.get("/api/man/cron")
@app.output(
    CronConfigSchema, status_code=200, description="Current cron configuration."
)
def get_cron():
    """Return the current cron configuration."""
    with _cron_lock:
        return {
            "inventory_interval": _cron_state["interval"],
            "page_refresh_interval": _cron_state["page_refresh_interval"],
        }, 200


@app.patch("/api/man/cron")
@app.input(CronConfigSchema)
@app.output(
    CronConfigSchema, status_code=200, description="Updated cron configuration."
)
def update_cron(json_data):
    """Update the cron configuration and persist it to disk."""
    inv = json_data.get("inventory_interval", 0)
    refresh = json_data.get("page_refresh_interval", 0)
    if inv < CRON_MIN_INTERVAL:
        return f"inventory_interval must be >= {CRON_MIN_INTERVAL} seconds", 400
    if refresh != 0 and refresh < 3:
        return "page_refresh_interval must be 0 (disabled) or >= 3 seconds", 400
    with _cron_lock:
        _cron_state["interval"] = inv
        _cron_state["page_refresh_interval"] = refresh
    _save_cron_config(inv, refresh)
    log.info("Cron updated: inventory=%ds page_refresh=%ds", inv, refresh)
    return {"inventory_interval": inv, "page_refresh_interval": refresh}, 200


@app.get("/api/man/agents")
def list_agents():
    """List all registered agents with their metadata."""
    result = []
    for eid in db.list_endpoints():
        data = db.get_endpoint(eid)
        if data is not None:
            result.append({"id": eid, **data})
    return jsonify(result), 200


@app.get("/api/man/agents/<agent_id>")
def get_agent(agent_id):
    """Get a single agent including full task history."""
    data = db.get_endpoint(agent_id)
    if data is None:
        return jsonify({"error": "unknown agent"}), 404
    return jsonify({"id": agent_id, **data}), 200


# END MANAGEMENT ROUTES


# ---------------------------------------------------------------------------
# Enrollment app (plain HTTP, port 8080)
# ---------------------------------------------------------------------------

enroll_app = PlainFlask(__name__ + ".enroll")


@enroll_app.post("/api/enroll")
def enroll():
    """
    One-shot unauthenticated endpoint.  Issues a CA-signed client cert and
    registers the agent.  Subsequent communication must use mTLS with this cert.
    """
    data = request.get_json(force=True) or {}
    hostname = data.get("hostname", "unknown")
    ip_address = request.remote_addr
    now = get_current_timestamp()

    # Reject if this host is already enrolled (same hostname + IP with a stored cert).
    for existing_id in db.list_endpoints():
        existing_data = db.get_endpoint(existing_id)
        if not existing_data:
            continue
        if (
            existing_data.get("hostname") == hostname
            and existing_data.get("ip_address") == ip_address
            and existing_data.get("cert_fingerprint")
        ):
            return jsonify({"error": "already enrolled, use existing cert"}), 409

    agent_id = generate_endpoint_id()
    client_cert, client_key = generate_client_cert(agent_id, _ca_cert, _ca_key)

    cert_der = client_cert.public_bytes(serialization.Encoding.DER)
    fingerprint = cert_fingerprint(cert_der)

    payload = {
        "hostname": hostname,
        "os": data.get("os", "unknown"),
        "os_name": data.get("os_name", "unknown"),
        "ip_address": ip_address,
        "registered_at": now,
        "last_seen": now,
        "cert_fingerprint": fingerprint,
        "blacklisted": False,
        "tasks": [],
    }
    db.save_endpoint(agent_id, payload)
    log.info("Enrolled new agent %s (%s @ %s)", agent_id, hostname, ip_address)

    cert_pem = client_cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = client_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    with open(CA_CERT_PATH, "r") as f:
        ca_cert_pem = f.read()

    return (
        jsonify(
            {
                "agent_id": agent_id,
                "cert_pem": cert_pem,
                "key_pem": key_pem,
                "ca_cert_pem": ca_cert_pem,
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    _ca_cert, _ca_key = ensure_ca_exists()
    ensure_server_cert_exists(_ca_cert, _ca_key)
    ensure_operator_cert_exists(_ca_cert, _ca_key)

    # Load persisted cron config and start the background worker
    saved = _load_cron_config()
    with _cron_lock:
        _cron_state["interval"] = saved["inventory_interval"]
        _cron_state["page_refresh_interval"] = saved["page_refresh_interval"]
    threading.Thread(target=_cron_worker, daemon=True).start()
    log.info("Cron worker started (inventory interval: %ds)", _cron_state["interval"])

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(SERVER_CERT_PATH, SERVER_KEY_PATH)
    ssl_ctx.verify_mode = ssl.CERT_REQUIRED
    ssl_ctx.load_verify_locations(CA_CERT_PATH)

    # Enrollment server — plain HTTP, daemon thread
    enroll_server = make_server("0.0.0.0", 8080, enroll_app)
    threading.Thread(target=enroll_server.serve_forever, daemon=True).start()
    log.info("Enrollment server listening on http://0.0.0.0:8080")

    # mTLS server — blocking main thread
    mtls_server = make_server(
        "0.0.0.0",
        8443,
        app,
        ssl_context=ssl_ctx,
        request_handler=MTLSRequestHandler,
    )
    log.info("mTLS server listening on https://0.0.0.0:8443")
    mtls_server.serve_forever()
