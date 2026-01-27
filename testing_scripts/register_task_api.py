import uuid
from datetime import datetime

import requests
from dateutil.tz import UTC

from datetime import datetime, UTC


def get_current_timestamp():
    """
    Returns current UTC time in ISO format with Z suffix.
    """
    current_utc_aware = datetime.now(UTC)
    return current_utc_aware.isoformat().replace("+00:00", "Z")


def generate_task_id():
    return str(uuid.uuid4())


def register_task(url, endpoint_id):

    # Parameters to be sent to the API
    params = {
        "agentid": endpoint_id,
        "task": {
            "id": generate_task_id(),
            "assigned_at": get_current_timestamp(),
            "instruction": "syscall",
            "arg": "ls -la",
        },
    }

    # Sending get request and saving the response as response object
    r = requests.post(url, json=params)

    # Print response
    print(r.text)


# Test it
register_task(
    "http://127.0.0.1:8443/api/man/post_task", "e92f0c72-efba-4739-b71c-6dbe39f86c56"
)
