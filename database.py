"""
Database module for managing endpoint data using TOML files.
"""

# standard library
import uuid
import os
from typing import Dict, Any, List, Optional

# pypi
import toml


def generate_endpoint_id():
    """
    Generates a unique endpoint ID using UUID4.
    """
    # We should check for collisions in a real implementation
    return str(uuid.uuid4())


class EndpointDatabase:
    """
    Database class for managing endpoint data stored in TOML files.
    """

    # Allowed fields for tasks, other keys will get stripped out when saving task data to ensure a consistent schema.
    _TASK_FIELDS = (
        "task_id",
        "assigned_at",
        "instruction",
        "arg",
        "exit_code",
        "stdout",
        "stderr",
        "stopped_processing_at",
        "responded",
        "inventory",
    )

    # Default values for task fields if they are missing in the input data.+
    _TASK_DEFAULTS: Dict[str, Any] = {
        "assigned_at": "",
        "instruction": "",
        "arg": "",
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "stopped_processing_at": "",
        "responded": False,
        "inventory": {},
    }

    def __init__(self):
        self.base_path = "data/"
        if not os.path.exists(self.base_path):
            os.makedirs(self.base_path, exist_ok=True)

    def _path_for_id(self, endpoint_id):
        """Returns the file path for a given endpoint ID."""
        return os.path.join(self.base_path, f"{str(endpoint_id)}")

    def save_endpoint(self, endpoint_id, data):
        """Saves endpoint data to a TOML file."""
        with open(self._path_for_id(endpoint_id), "w", encoding="utf-8") as f:
            f.write(toml.dumps(data))

    def endpoint_exists(self, endpoint_id):
        """
        Checks if an endpoint exists by verifying the presence of its TOML file.

        :param self: The instance of the class.
        :param endpoint_id: The unique identifier for the endpoint.
        """
        return os.path.exists(self._path_for_id(endpoint_id))

    def list_endpoints(self):
        """Lists all registered endpoint IDs."""
        all_files = os.listdir(self.base_path)
        return [f.replace("", "") for f in all_files if f.endswith("")]

    def get_endpoint(self, endpoint_id):
        """Retrieves endpoint data from its TOML file."""

        if not self.endpoint_exists(endpoint_id):
            return None
        with open(self._path_for_id(endpoint_id), "r", encoding="utf-8") as f:
            data = toml.load(f)

        # Normalize task objects so downstream callers always get the expected schema.
        self._normalize_endpoint_tasks(data)

        return data

    def ensure_non_duplicate(self, new_endpoint_id, new_info):
        """Returns True if no other endpoint has the same hostname and IP."""
        new_hostname = new_info.get("hostname")
        nip = new_info.get("ip_address")

        for eid in self.list_endpoints():
            if eid == new_endpoint_id:
                continue
            data = self.get_endpoint(eid)
            if data is None:
                continue
            if data.get("hostname") == new_hostname and data.get("ip_address") == nip:
                return False
        return True

    def register_endpoint(self, agent_id, info):
        """Registers a new endpoint if it is not a duplicate."""
        if not self.ensure_non_duplicate(agent_id, info):
            return False

        # Ensure task list exists in stored data.
        if "tasks" not in info:
            info["tasks"] = []

        self.save_endpoint(agent_id, info)
        return True

    def add_task(self, endpoint_id, task):
        """
        Adds a task to the specified endpoint's task list.

        :param self: The instance of the class.
        :param endpoint_id: The unique identifier for the endpoint.
        :param task: The task to be added.
        """
        data = self.get_endpoint(endpoint_id)
        if data is None:
            return False
        if "tasks" not in data:
            data["tasks"] = []
        try:
            sanitized_task = self._sanitize_task(task, responded=False)
        except ValueError:
            return False
        data["tasks"].append(sanitized_task)
        self.save_endpoint(endpoint_id, data)
        return True

    def post_task_result(self, endpoint_id, tdata):
        """
        Posts the result of a task for a specific endpoint.

        :param self: The instance of the class.
        :param endpoint_id: The unique identifier for the endpoint.
        :param task_id: The unique identifier for the task.
        :param result: The result of the task.
        """
        data = self.get_endpoint(endpoint_id)
        if data is None:
            return False

        try:
            sanitized_result = self._sanitize_task(tdata, responded=True)
        except ValueError:
            return False
        task_id = sanitized_result["task_id"]

        provided_fields = set(tdata.keys())
        provided_fields.discard("task_id")

        updated = False
        for task in data.get("tasks", []):
            # Handle legacy tasks that might still use "id" as the identifier.
            if task.get("task_id") is None and task.get("id") == task_id:
                task["task_id"] = task.pop("id")

            if task.get("task_id") == task_id:
                for key, value in sanitized_result.items():
                    if key == "task_id":
                        continue
                    if key == "responded" or key in provided_fields:
                        task[key] = value
                updated = True
                break
        else:
            return False  # Task ID not found

        # Remove legacy top-level entries that stored task results separately.
        if isinstance(data.get(task_id), dict):
            data.pop(task_id, None)

        self.save_endpoint(endpoint_id, data)
        return True

    def get_tasks_for_endpoint(self, endpoint_id):
        """
        Retrieves the list of tasks for a specific endpoint.

        :param self: The instance of the class.
        :param endpoint_id: The unique identifier for the endpoint.
        """
        data = self.get_endpoint(endpoint_id)
        if data is None:
            return None
        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            return []
        return [task for task in tasks if not task.get("responded", False)]

    def _sanitize_task(
        self, task: Dict[str, Any], *, responded: Optional[bool] = None
    ) -> Dict[str, Any]:
        """Return a copy of task data limited to the known task schema."""
        if not isinstance(task, dict):
            raise ValueError("task must be a dictionary")

        sanitized: Dict[str, Any] = {}
        task_id = task.get("task_id") or task.get("id")
        if not task_id:
            raise ValueError("task is missing task_id")
        sanitized["task_id"] = task_id

        for field in self._TASK_FIELDS:
            if field == "task_id":
                continue
            if field in task:
                sanitized[field] = task[field]

        for field, default in self._TASK_DEFAULTS.items():
            sanitized.setdefault(field, default)

        if responded is not None:
            sanitized["responded"] = responded

        return sanitized

    def _normalize_endpoint_tasks(self, data: Dict[str, Any]) -> None:
        """Ensure every stored task follows the canonical schema."""
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            data["tasks"] = []
            return

        normalized: List[Dict[str, Any]] = []
        seen_task_ids = set()
        for task in tasks:
            try:
                normalized_task = self._sanitize_task(task)
            except ValueError:
                # Skip malformed tasks rather than breaking the call chain.
                continue
            normalized.append(normalized_task)
            seen_task_ids.add(normalized_task["task_id"])

        data["tasks"] = normalized

        # Clean up any legacy entries stored outside the tasks list.
        for task_id in list(seen_task_ids):
            if isinstance(data.get(task_id), dict):
                data.pop(task_id, None)


# Basic tests
if __name__ == "__main__":
    e = EndpointDatabase()
    # sample_agent_id = e.register_endpoint(
    #    input("IP: "),
    #    input("Hostname: "),
    #    input("OS Type: "),
    #    input("OS: "),
    #    input("Last seen: "),
    # )
    # print("Registered ID:", sample_agent_id)
    # e.add_task(sample_agent_id, {"id": "task1", "command": "ls"})
    # tasks = e.get_tasks_for_endpoint(sample_agent_id)
    # print("Tasks for endpoint:", tasks)
