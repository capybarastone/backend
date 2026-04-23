"""
Backend flask app for handling endpoint check-ins and task results.
"""

# standard library
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
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID
from flask import Flask as PlainFlask
from flask import jsonify, request
from marshmallow import INCLUDE
from werkzeug.serving import make_server

from certs import (
    CA_CERT_PATH,
    SERVER_CERT_PATH,
    SERVER_KEY_PATH,
    cert_fingerprint,
    ensure_ca_exists,
    ensure_operator_cert_exists,
    ensure_server_cert_exists,
    generate_client_cert,
    MTLSRequestHandler,
)
from database import EndpointDatabase, generate_endpoint_id
from util import get_current_timestamp, primitive_log

log = logging.getLogger(__name__)

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
@app.before_request
def _restrict_management_to_loopback():
    if request.path.startswith("/api/man"):
        try:
            addr = ipaddress.ip_address(request.remote_addr)
        except ValueError:
            return "forbidden", 403
        if not addr.is_loopback:
            return "forbidden", 403


@app.post("/api/man/post_task")
@app.input(PostTaskSchema)
@app.output(StatusSchema, status_code=200, description="Task acceptance confirmation.")
def post_task(json_data):
    """Create a task for the given agent."""
    agent_id = json_data["agentid"]
    task_payload = json_data["task"]

    # TODO: maintain a canonical list of valid instructions and return 400 for unknown ones.
    # Should mirror the cases in endpoint/task_utils.go: syscall, exit, inventory, install_av, av_scan.

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
