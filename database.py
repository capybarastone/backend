"""
Database module for managing endpoint data using TOML files.
"""

# standard library
import uuid
import os

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
        """Registers a new endpoint if it is not a duplicate.
        Returns (True, None) on success, (False, reason) on failure."""
        if not self.ensure_non_duplicate(agent_id, info):
            return False, "duplicate endpoint"
        self.save_endpoint(agent_id, info)
        return True, None

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
        data["tasks"].append(task)
        self.save_endpoint(endpoint_id, data)
        return True

    def post_task_result(self, endpoint_id, task_id, result):
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

        for task in data.get("tasks", []):
            if task.get("id") == task_id:
                task["result"] = result
                break
        else:
            return False  # Task ID not found

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
        return data.get("tasks", [])


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
