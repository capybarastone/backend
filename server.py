"""
Backend flask app for handling endpoint check-ins and task results.
"""

import logging
import sys
from datetime import datetime, UTC

from flask import Flask, request, jsonify
from database import EndpointDatabase

app = Flask(__name__)

db = EndpointDatabase()


def get_current_timestamp():
    """
    Returns current UTC time in ISO format with Z suffix.
    """
    current_utc_aware = datetime.now(UTC)
    return current_utc_aware.isoformat().replace("+00:00", "Z")


@app.route("/")
def index():
    """
    Docstring for index
    """
    # TODO: check user agent and tell browsers to get bent
    return ("you're a human. go away. this is an api", 400)


@app.route("/api/checkin", methods=["POST"])
def checkin():
    """
    Docstring for checkin
    """
    agentid = request.args.get("agentid")
    if agentid is None:
        return ("agentid query parameter required", 400)

    endpoint = db.get_endpoint(agentid)

    if endpoint is None:
        return ("unknown agentid", 404)

    last_seen = get_current_timestamp()
    endpoint["last_seen"] = last_seen
    db.save_endpoint(agentid, endpoint)

    if db.get_tasks_for_endpoint(agentid):
        return jsonify(db.get_tasks_for_endpoint(agentid))
    else:
        return ("no tasks", 204)


@app.route("/api/post_result", methods=["POST"])
def post_result():
    """
    Docstring for post_result
    """
    data = request.json
    agentid = data.get("agentid")
    task_id = data.get("task_id")
    result = data.get("result")

    if not agentid or not task_id or result is None:
        return ("missing parameters", 400)

    success = db.post_task_result(agentid, task_id, result)
    if not success:
        return ("failed to post result", 400)

    return ("result posted", 200)


@app.route("/api/register", methods=["POST"])
def register_endpoint():
    """
    Docstring for register_endpoint
    """
    info = request.json

    if not info:
        return ("missing parameters", 400)

    app.logger.info(
        "Incoming %s request to %s from %s with payload %s",
        request.method,
        request.path,
        request.remote_addr,
        info,
    )

    agentid = db.generate_endpoint_id()

    info["ip_address"] = request.remote_addr
    info["registered_at"] = get_current_timestamp()
    info["last_seen"] = info["registered_at"]

    success = db.register_endpoint(agentid, info)
    if not success:
        return ("failed to register endpoint. probably a duplicate?", 400)

    return (jsonify({"agent_id": agentid}), 200)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443, debug=True)
