"""
Backend flask app for handling endpoint check-ins and task results.
"""

from flask import request, jsonify
from apiflask import APIFlask

from database import EndpointDatabase, generate_endpoint_id
from util import get_current_timestamp, primitive_log

app = APIFlask(__name__)

db = EndpointDatabase()


# START ENDPOINT ROUTES
@app.post("/api/end/checkin")
def checkin():
    """Check in an endpoint and retrieve any queued tasks.
    ---
    summary: Endpoint check-in
    description: Use this endpoint to update the endpoint heartbeat and fetch pending tasks.
    parameters:
      - in: query
        name: agentid
        description: Unique identifier for the endpoint.
        required: true
        schema:
          type: string
    responses:
      200:
        description: A JSON array of tasks assigned to the endpoint.
        content:
          application/json:
            schema:
              type: array
              items:
                type: object
      204:
        description: No tasks are available for the endpoint.
      400:
        description: The required agent ID was not provided.
      404:
        description: The supplied agent ID does not exist.
    """
    agentid = request.args.get("agentid")
    if agentid is None:
        return "agentid query parameter required", 400

    endpoint = db.get_endpoint(agentid)

    if endpoint is None:
        return "unknown agentid", 404

    last_seen = get_current_timestamp()
    endpoint["last_seen"] = last_seen
    db.save_endpoint(agentid, endpoint)

    if db.get_tasks_for_endpoint(agentid):
        return jsonify(db.get_tasks_for_endpoint(agentid))
    return "no tasks", 204


@app.post("/api/end/post_result")
def post_result():
    """Submit the outcome of a task that has been executed by an endpoint.
    ---
    summary: Submit task result
    description: Provide the task result payload for a task that has been executed by the endpoint.
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              agent_id:
                type: string
                description: Unique identifier for the endpoint that completed the task.
              task_id:
                type: string
                description: Unique identifier for the task being reported.
              assigned_at:
                type: string
              instruction:
                type: string
              arg:
                type: string
              exit_code:
                type: integer
              stdout:
                type: string
              stderr:
                type: string
              stopped_processing_at:
                type: string
              responded:
                type: boolean
            required:
              - agent_id
              - task_id
    responses:
      200:
        description: The task result was stored successfully.
      400:
        description: Missing parameters or failure persisting the result.
    """
    data = request.json or {}
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

    return jsonify({"status": "result posted"}), 200


@app.post("/api/end/register")
def register_endpoint():
    """Register a new endpoint and obtain its assigned agent identifier.
    ---
    summary: Register endpoint
    description: Submit endpoint metadata to register the endpoint and receive an agent id.
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            additionalProperties: true
            description: Endpoint metadata: hostname, operating system, etc.
    responses:
      200:
        description: Registration was successful and an agent identifier is returned.
        content:
          application/json:
            schema:
              type: object
              properties:
                agent_id:
                  type: string
                  description: Unique identifier assigned to the endpoint.
      400:
        description: Missing payload data or duplicate registration prevented completion.
    """
    payload = request.json
    if not payload:
        return "missing parameters", 400

    remote_addr = request.remote_addr
    now = get_current_timestamp()

    app.logger.info(
        "Incoming %s request to %s from %s with payload %s",
        request.method,
        request.path,
        remote_addr,
        payload,
    )

    agent_id = generate_endpoint_id()
    payload["ip_address"] = remote_addr
    payload["registered_at"] = now
    payload["last_seen"] = now

    if not db.register_endpoint(agent_id, payload):
        return "failed to register endpoint. probably a duplicate?", 400

    return jsonify({"agent_id": agent_id}), 200


# END ENDPOINT ROUTES


# START MANAGEMENT ROUTES
@app.post("/api/man/post_task")
def post_task():
    """
    Create a task for the given agent.

    Expects JSON body with `agentid` and `task`.
    Returns 200 on success, 400 for missing parameters or add failure,
    and 404 for unknown agentid.
    """
    body = request.json
    task = body.get("task")
    if task is None:
        return "missing parameters", 400
    endpoint = db.get_endpoint(body["agentid"])
    if endpoint is None:
        return "unknown agentid", 404
    res = db.add_task(body["agentid"], task)
    if not res:
        return "failed to add task", 400
    return "success", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443, debug=True)
