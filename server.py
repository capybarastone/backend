"""
Backend flask app for handling endpoint check-ins and task results.
"""

from flask import request
from apiflask import APIFlask, Schema
from apiflask.fields import String, Integer, Boolean, Dict, Raw
from marshmallow import INCLUDE

from database import EndpointDatabase, generate_endpoint_id
from util import get_current_timestamp, primitive_log

app = APIFlask(__name__)

db = EndpointDatabase()


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
    arg = String(metadata={"description": "Argument associated with the instruction."})
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
def register_endpoint(payload):
    """Register a new endpoint and obtain its assigned agent identifier."""
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

    if not db.register_endpoint(agent_id, dict(payload)):
        return "failed to register endpoint. probably a duplicate?", 400

    return {"agent_id": agent_id}, 200


# END ENDPOINT ROUTES


# START MANAGEMENT ROUTES
@app.post("/api/man/post_task")
@app.input(PostTaskSchema)
@app.output(StatusSchema, status_code=200, description="Task acceptance confirmation.")
def post_task(body):
    """Create a task for the given agent."""
    endpoint = db.get_endpoint(body["agentid"])
    if endpoint is None:
        return "unknown agentid", 404
    res = db.add_task(body["agentid"], body["task"])
    if not res:
        return "failed to add task", 400
    return {"status": "success"}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443, debug=True)
