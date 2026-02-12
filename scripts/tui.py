#!/usr/bin/env python3
"""Interactive TUI for assigning management tasks to endpoints.

Features:
    • Multi-select interface for choosing endpoints.
    • Guided task selection with sensible defaults.
    • Colored live dashboard that monitors task completion in real time.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

import requests

try:
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice
except ImportError:  # pragma: no cover - defensive guard for missing optional deps
    print(
        "InquirerPy is required for the interactive prompts. "
        "Install it with `pip install InquirerPy` or `pip install -e .[dev]`.",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:  # pragma: no cover - defensive guard for missing optional deps
    print(
        "Rich is required for the colored dashboard. "
        "Install it with `pip install rich` or `pip install -e .[dev]`.",
        file=sys.stderr,
    )
    sys.exit(1)

from requests import Response

# Ensure project root is on sys.path when the script is invoked directly.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import EndpointDatabase

console = Console()


DEFAULT_MANAGEMENT_URL = "http://127.0.0.1:8443/api/man/post_task"


def current_timestamp() -> str:
    """Return current UTC time in ISO 8601 format with trailing Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_task_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
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
        payload = {
            "task_id": self.task_id or new_task_id(),
            "assigned_at": current_timestamp(),
            "instruction": self.instruction,
            "arg": self.arg if self.arg is not None else "",
        }
        return payload


def post_task(url: str, agent_id: str, task_payload: Dict[str, Any]) -> Response:
    body = {"agentid": agent_id, "task": task_payload}
    return requests.post(url, json=body, timeout=15)


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive tool for assigning management tasks to endpoints."
    )
    parser.add_argument(
        "--agent-id",
        dest="agent_ids",
        action="append",
        help="Agent identifier(s) to target. Omit to choose interactively.",
    )
    parser.add_argument(
        "--task",
        choices=sorted(TASK_PRESETS.keys()),
        help="Pre-select a task preset. Omit for interactive selection.",
    )
    parser.add_argument(
        "--instruction",
        help="Override the instruction instead of using a predefined --task template.",
    )
    parser.add_argument(
        "--arg",
        help="Optional argument for the task. Prompts interactively when required.",
    )
    parser.add_argument(
        "--task-id",
        help="Optional task UUID. When targeting multiple endpoints a UUID is generated per endpoint.",
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
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between dashboard refreshes while monitoring (default: %(default)s).",
    )
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="Skip live monitoring of assigned tasks.",
    )
    return parser.parse_args(argv)


def prompt_for_endpoints(db: EndpointDatabase) -> List[str]:
    """Present a multi-select prompt for available endpoints."""
    endpoints = db.list_endpoints()
    if not endpoints:
        console.print(
            "[red]No registered endpoints were found in the local database.[/red]"
        )
        console.print("Add agents by calling /api/end/register before using this tool.")
        sys.exit(1)

    choices: List[Choice] = []
    for endpoint_id in sorted(endpoints):
        data = db.get_endpoint(endpoint_id) or {}
        hostname = data.get("hostname") or "unknown-host"
        ip_address = data.get("ip_address") or "unknown-ip"
        last_seen = data.get("last_seen") or "never"
        label = f"{endpoint_id[:8]} {hostname} " f"{ip_address} • last seen {last_seen}"
        choices.append(Choice(value=endpoint_id, name=label))

    console.print(
        Panel.fit(
            "[bold]Select one or more endpoints[/bold]\n"
            "[dim]Use arrow keys to navigate, space to toggle, enter to confirm.[/dim]"
        )
    )
    selected = inquirer.checkbox(
        message="Endpoints:",
        choices=choices,
        instruction="space to toggle",
        vi_mode=False,
    ).execute()

    if not selected:
        console.print("[yellow]No endpoints selected. Aborting.[/yellow]")
        sys.exit(0)

    return selected


def prompt_for_task(args: argparse.Namespace) -> tuple[str, str | None]:
    """Resolve the task instruction and optional argument."""
    if args.instruction:
        return args.instruction, args.arg

    preset_key = args.task
    if not preset_key:
        console.print(
            Panel.fit(
                "[bold]Choose a task preset[/bold]\n"
                "[dim]You can also provide --instruction for arbitrary tasks.[/dim]"
            )
        )
        preset_key = inquirer.select(
            message="Task preset:",
            choices=[
                Choice(value=key, name=f"{key} — {preset.instruction}")
                for key, preset in TASK_PRESETS.items()
            ],
        ).execute()

    preset = TASK_PRESETS[preset_key]
    arg = args.arg
    if preset.requires_arg:
        if arg is None:
            default_hint = (
                f" (default: {preset.default_arg})" if preset.default_arg else ""
            )
            arg = inquirer.text(
                message=f"Argument for {preset_key}{default_hint}:",
                default=preset.default_arg or "",
            ).execute()
            if arg == "" and preset.default_arg is None:
                console.print(f"[red]The {preset_key} task requires an argument.[/red]")
                return prompt_for_task(args)
            if arg == "":
                arg = preset.default_arg
    else:
        if arg is not None:
            console.print(
                f"[yellow]The {preset_key} task ignores the provided argument.[/yellow]"
            )
            arg = None

    return preset.instruction, arg


def dispatch_tasks(
    agent_ids: Iterable[str],
    builder: TaskBuilder,
    *,
    url: str,
    print_payload: bool,
) -> Dict[str, str]:
    """Queue the task for each endpoint and return mapping of agent_id -> task_id."""
    assignments: Dict[str, str] = {}
    for agent_id in agent_ids:
        payload = builder.build()
        assignments[agent_id] = payload["task_id"]

        if print_payload:
            console.print(
                Panel.fit(
                    json.dumps({"agentid": agent_id, "task": payload}, indent=2),
                    title=f"Payload for {agent_id}",
                    border_style="blue",
                )
            )

        try:
            response = post_task(url, agent_id, payload)
        except requests.RequestException as exc:
            console.print(
                f"[red]Failed to connect to management API for {agent_id}: {exc}[/red]"
            )
            continue

        if response.status_code != 200:
            content = response.text.strip() or "no response body"
            console.print(
                f"[red]Task rejected for {agent_id}: {response.status_code} — {content}[/red]"
            )
            continue

        console.print(
            f"[green]Queued task {payload['task_id']} for endpoint {agent_id}.[/green]"
        )

    return assignments


def summarize_task_state(task: Mapping[str, Any]) -> tuple[Text, str, str, str]:
    """Return formatted status text, exit code, stdout preview, and timestamp."""
    responded = task.get("responded")
    exit_code = task.get("exit_code")
    stopped_at = task.get("stopped_processing_at") or ""

    if not responded:
        status = Text("Pending", style="yellow")
    else:
        if exit_code is None:
            status = Text("Completed", style="green")
        elif exit_code == 0:
            status = Text("Success", style="green")
        else:
            status = Text(f"Failed ({exit_code})", style="red")

    stdout = task.get("stdout") or str(task.get("inventory")) or ""
    stderr = task.get("stderr") or ""
    if stderr and not stdout:
        stdout = f"[stderr] {stderr}"

    snippet = stdout.replace("\n", " ")  # [:80]
    return status, str(exit_code) if exit_code is not None else "-", snippet, stopped_at


def monitor_tasks(
    db: EndpointDatabase,
    assignments: Dict[str, str],
    poll_interval: float,
) -> None:
    """Render a live table showing task completion for each endpoint."""
    if not assignments:
        console.print("[yellow]No tasks were queued; skipping monitoring.[/yellow]")
        return

    console.print(
        Panel.fit(
            "Monitoring task execution.\n"
            "[dim]Press Ctrl+C to stop watching. The dashboard exits automatically once all tasks respond.[/dim]",
            title="Live task monitor",
            border_style="cyan",
        )
    )

    with Live(console=console, refresh_per_second=4) as live:
        try:
            while True:
                table = Table(title="Endpoint task state", expand=True)
                table.add_column("Endpoint", style="bold cyan")
                table.add_column("Task ID")
                table.add_column("Status")
                table.add_column("Exit Code")
                table.add_column("Last Update")
                table.add_column("Output Preview")

                all_done = True

                for agent_id, task_id in assignments.items():
                    endpoint_data = db.get_endpoint(agent_id)
                    if endpoint_data is None:
                        table.add_row(
                            agent_id,
                            task_id,
                            Text("Missing endpoint record", style="red"),
                            "-",
                            "-",
                            "",
                        )
                        continue

                    tasks = endpoint_data.get("tasks", [])
                    task = next(
                        (item for item in tasks if item.get("task_id") == task_id),
                        None,
                    )

                    if task is None:
                        table.add_row(
                            agent_id,
                            task_id,
                            Text("Awaiting queue", style="yellow"),
                            "-",
                            "-",
                            "",
                        )
                        all_done = False
                        continue

                    status, exit_code, snippet, stopped_at = summarize_task_state(task)
                    if not task.get("responded"):
                        all_done = False

                    last_update = stopped_at or task.get("assigned_at") or "-"
                    table.add_row(
                        agent_id,
                        task_id,
                        status,
                        exit_code,
                        last_update,
                        snippet,
                    )

                live.update(table)

                if all_done:
                    break
                time.sleep(max(poll_interval, 0.5))
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped monitoring at user request.[/yellow]")


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    db = EndpointDatabase()

    agent_ids = args.agent_ids or prompt_for_endpoints(db)

    instruction, arg = prompt_for_task(args)

    if args.task_id and len(agent_ids) > 1:
        console.print(
            "[yellow]Multiple endpoints selected; generating unique task IDs per endpoint.[/yellow]"
        )
        task_id = None
    else:
        task_id = args.task_id

    builder = TaskBuilder(instruction=instruction, arg=arg, task_id=task_id)

    assignments = dispatch_tasks(
        agent_ids,
        builder,
        url=args.url,
        print_payload=args.print_payload,
    )

    if not args.no_monitor:
        monitor_tasks(db, assignments, args.poll_interval)

    console.print("[bold green]Done.[/bold green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
