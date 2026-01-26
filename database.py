"""
Database module for managing endpoint data using TOML files.
"""

# standard library
import uuid
import os

# pypi
import toml


class EndpointDatabase:
    """
    Database class for managing endpoint data stored in TOML files.
    """

    def __init__(self):
        self.base_path = "data/"
        if not os.path.exists(self.base_path):
            os.makedirs(self.base_path, exist_ok=True)

    def generate_endpoint_id(self):
        # We should check for collisions in a real implementation
        return str(uuid.uuid4())

    def _path_for_id(self, endpoint_id):
        return os.path.join(self.base_path, f"{str(endpoint_id)}.toml")

    def save_endpoint(self, endpoint_id, data):
        with open(self._path_for_id(endpoint_id), "w") as f:
            f.write(toml.dumps(data))

    def endpoint_exists(self, endpoint_id):
        return os.path.exists(self._path_for_id(endpoint_id))

    def list_endpoints(self):
        all_files = os.listdir(self.base_path)
        return [f.replace(".toml", "") for f in all_files if f.endswith(".toml")]

    def get_endpoint(self, endpoint_id):
        if not self.endpoint_exists(endpoint_id):
            return None

        with open(self._path_for_id(endpoint_id), "r") as f:
            data = toml.load(f)

        return data

    def ensure_non_duplicate(self, new_endpoint_id, new_info):
        """Returns True if no other endpoint has the same hostname and IP."""
        nhostname = new_info.get("hostname")
        nip = new_info.get("ip_address")

        for eid in self.list_endpoints():
            if eid == new_endpoint_id:
                continue
            data = self.get_endpoint(eid)
            if data is None:
                continue
            if data.get("hostname") == nhostname and data.get("ip_address") == nip:
                return False
        return True

    def register_endpoint(self, agent_id, info):
        """Registers a new endpoint if it is not a duplicate.
        Returns (True, None) on success, (False, reason) on failure."""
        if not self.ensure_non_duplicate(agent_id, info):
            return (False, "duplicate endpoint")
        self.save_endpoint(agent_id, info)
        return (True, None)

    def add_task(self, endpoint_id, task):
        data = self.get_endpoint(endpoint_id)
        if data is None:
            return False

        data["tasks"].append(task)
        self.save_endpoint(endpoint_id, data)
        return True

    def post_task_result(self, endpoint_id, task_id, result):
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
        data = self.get_endpoint(endpoint_id)
        if data is None:
            return None
        return data.get("tasks", [])


# Basic tests
if __name__ == "__main__":
    e = EndpointDatabase()
    eid = e.register_endpoint(
        input("IP: "),
        input("Hostname: "),
        input("OS Type: "),
        input("OS: "),
        input("Last seen: "),
    )
    print("Registered ID:", eid)
    e.add_task(eid, {"id": "task1", "command": "ls"})
    tasks = e.get_tasks_for_endpoint(eid)
    print("Tasks for endpoint:", tasks)
