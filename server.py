"""
Backend flask app for handling endpoint check-ins and task results.
"""

from datetime import datetime, UTC

from flask import request, jsonify
from apiflask import APIFlask
from database import EndpointDatabase

app = APIFlask(__name__)

db = EndpointDatabase()


def get_current_timestamp():
    """
    Returns current UTC time in ISO format with Z suffix.
    """
    current_utc_aware = datetime.now(UTC)
    return current_utc_aware.isoformat().replace("+00:00", "Z")


@app.post("/api/checkin")
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
        return ("agentid query parameter required", 400)

    endpoint = db.get_endpoint(agentid)

    if endpoint is None:
        return ("unknown agentid", 404)

    last_seen = get_current_timestamp()
    endpoint["last_seen"] = last_seen
    db.save_endpoint(agentid, endpoint)

    if db.get_tasks_for_endpoint(agentid):
        return jsonify(db.get_tasks_for_endpoint(agentid))
    return ("no tasks", 204)


@app.post("/api/post_result")
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
              agentid:
                type: string
                description: Unique identifier for the endpoint that completed the task.
              task_id:
                type: string
                description: Unique identifier for the task being reported.
              result:
                description: Arbitrary JSON payload containing the task execution result.
            required:
              - agentid
              - task_id
              - result
    responses:
      200:
        description: The task result was stored successfully.
      400:
        description: Missing parameters or failure persisting the result.
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


@app.post("/api/register")
def register_endpoint():
    """Register a new endpoint and obtain its assigned agent identifier.
    ---
    summary: Register endpoint
    description: Submit endpoint metadata to register the endpoint and receive an agent identifier for future requests.
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            additionalProperties: true
            description: Endpoint metadata such as hostname, operating system, or other identifying attributes.
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
