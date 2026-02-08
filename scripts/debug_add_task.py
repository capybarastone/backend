#!/usr/bin/env python3
"""Utility script for posting management tasks via the REST API.

Example (syscall):
    python backend/scripts/register_task_api.py \\
        --agent-id <endpoint uuid> \\
        --task syscall \\
        --arg "whoami"

Example (inventory):
    python backend/scripts/register_task_api.py \\
        --agent-id <endpoint uuid> \\
        --task inventory
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict

import requests

DEFAULT_MANAGEMENT_URL = "http://127.0.0.1:8443/api/man/post_task"


def current_timestamp() -> str:
    """Return current UTC time in ISO 8601 format with trailing Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_task_id() -> str:
    return str(uuid.uuid4())


@dataclass
class TaskPreset:
    instruction: str
    requires_arg: bool
    default_arg: str | None = None


TASK_PRESETS: Dict[str, TaskPreset] = {
    "syscall": TaskPreset("syscall", True, "ls -la"),
    "inventory": TaskPreset("inventory", False),
    "exit": TaskPreset("exit", False),
}


@dataclass
class TaskBuilder:
    instruction: str
    arg: str | None
    task_id: str | None = None

    def build(self) -> Dict[str, Any]:
        """Return a task payload that conforms to backend/STRUCTS.md."""
        return {
            "task_id": self.task_id or new_task_id(),
            "assigned_at": current_timestamp(),
            "instruction": self.instruction,
            "arg": self.arg,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "stopped_processing_at": "",
            "responded": False,
        }


def post_task(
    url: str, agent_id: str, task_payload: Dict[str, Any]
) -> requests.Response:
    body = {"agentid": agent_id, "task": task_payload}
    response = requests.post(url, json=body, timeout=15)
    return response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a debug task for an endpoint.")
    parser.add_argument(
        "--agent-id",
        required=True,
        help="Agent identifier returned by /api/end/register.",
    )
    parser.add_argument(
        "--task",
        choices=sorted(TASK_PRESETS.keys()),
        default="syscall",
        help="Task template to use when --instruction is omitted (default: %(default)s).",
    )
    parser.add_argument(
        "--arg",
        help="Optional argument for the task. Required when the selected task expects one.",
    )
    parser.add_argument(
        "--instruction",
        help="Override the instruction instead of using a predefined --task template.",
    )
    parser.add_argument(
        "--task-id",
        help="Optional task UUID. Generated automatically when omitted.",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_MANAGEMENT_URL,
        help=f"Management API endpoint (default: {DEFAULT_MANAGEMENT_URL}).",
    )
    parser.add_argument(
        "--print-payload",
        action="store_true",
        help="Print the outgoing JSON payload before posting.",
    )

    args = parser.parse_args(argv)

    if args.instruction:
        instruction = args.instruction
        arg = args.arg
    else:
        preset = TASK_PRESETS[args.task]
        if preset.requires_arg:
            arg = args.arg if args.arg is not None else preset.default_arg
            if arg is None:
                parser.error(f"--arg is required when using the {args.task!r} task.")
        else:
            if args.arg is not None:
                parser.error(f"--arg is not supported for the {args.task!r} task.")
            arg = preset.default_arg
        instruction = preset.instruction

    task_payload = TaskBuilder(
        instruction=instruction,
        arg=arg,
        task_id=args.task_id,
    ).build()

    if args.print_payload:
        json.dump(
            {"agentid": args.agent_id, "task": task_payload}, sys.stdout, indent=2
        )
        sys.stdout.write("\n")

    response = post_task(args.url, args.agent_id, task_payload)

    print(f"Status: {response.status_code}")
    content_type = response.headers.get("Content-Type", "")

    if "application/json" in content_type:
        try:
            print(json.dumps(response.json(), indent=2))
        except ValueError:
            print(response.text)
    else:
        print(response.text)

    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
