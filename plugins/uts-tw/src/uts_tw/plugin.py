import json
import subprocess
from datetime import datetime, timedelta
from typing import List

import typer
from taskw import TaskWarrior

from universal_task_sync.models import Priority, TaskCIR, TaskStatus


class TaskwarriorPlugin:
    """Strictly handles TW <-> CIF translation and IO."""

    def set_filter(self, filter):
        self.target = filter
        parts = filter.split()
        result = {"tags": []}

        for item in parts:
            if item.startswith("+"):
                # Remove the '+' and add to tags list
                result["tags"].append(item[1:])
            elif ":" in item:
                # Split by the first colon found
                key, value = item.split(":", 1)
                result[key] = value
                self.filter = result

    @property
    def name(self) -> str:
        return "tw"

    def authenticate(self) -> bool:
        return True

    def to_cif(self, raw: dict) -> TaskCIR:
        """Translate TW JSON dict to CIF."""
        # Status Mapping
        status_map = {
            "pending": TaskStatus.PENDING,
            "completed": TaskStatus.COMPLETED,
            "deleted": TaskStatus.DELETED,
            "waiting": TaskStatus.WAITING,
        }

        # Date Helper
        def p_date(d_str):
            if not d_str:
                return None
            return datetime.strptime(d_str, "%Y%m%dT%H%M%SZ")

        # Duration Helper
        def p_dur(dur_str):
            if not dur_str:
                return None
            # Basic parsing: '2h' -> timedelta
            try:
                if "h" in dur_str:
                    return timedelta(hours=float(dur_str.replace("h", "")))
                if "d" in dur_str:
                    return timedelta(days=float(dur_str.replace("d", "")))
            except:
                pass
            return None

        return TaskCIR(
            uuid=raw.get("uuid"),
            tool_uid=raw.get("uuid"),
            last_modified=p_date(raw.get("modified")) or datetime.now(),
            description=raw.get("description", ""),
            body="\n".join([a["description"] for a in raw.get("annotations", [])]),
            project=raw.get("project"),
            status=status_map.get(raw.get("status"), TaskStatus.PENDING),
            priority=Priority(raw.get("priority")) if raw.get("priority") in ["H", "M", "L"] else None,
            tags=raw.get("tags", []),
            start=p_date(raw.get("start")),
            due=p_date(raw.get("due")),
            scheduled=p_date(raw.get("scheduled")),
            effort=p_dur(raw.get("effort")),
            progress=int(raw.get("percentage", 0)),
            depends=raw.get("depends", []),
            owner=raw.get("owner"),
        )

    def from_cif(self, task: TaskCIR) -> dict:
        """Translate CIF to TW JSON dict."""

        def f_date(dt):
            return dt.strftime("%Y%m%dT%H%M%SZ") if dt else None

        tw_dict = {
            "uuid": task.uuid,
            "description": task.description,
            "project": task.project,
            "status": task.status.value,
            "tags": task.tags,
            "priority": task.priority.value if task.priority else None,
            "start": f_date(task.start),
            "due": f_date(task.due),
            "scheduled": f_date(task.scheduled),
            "depends": task.depends,
        }

        # Add annotations for the body if content exists
        if task.body:
            tw_dict["annotations"] = [{"entry": f_date(datetime.now()), "description": task.body}]

        # Convert effort timedelta back to string (e.g., '2.0h')
        if task.effort:
            tw_dict["effort"] = f"{task.effort.total_seconds() / 3600}h"
        if self.filter["project"]:
            tw_dict["project"] = self.filter["project"]
        tw_dict["tags"].extend(self.filter["tags"])
        if len(tw_dict) > 0:
            tw_dict["tags"] = list(set(tw_dict["tags"]))

        return {k: v for k, v in tw_dict.items() if v is not None and (not isinstance(v, list) or len(v) > 0)}

    def fetch_one(self, tool_uid: str) -> dict:
        w = TaskWarrior()
        _, tw_task = w.get_task(uuid=tool_uid)
        return tw_task

    def fetch_raw(self) -> List[dict]:
        """IO: Export from Taskwarrior."""
        cmd = ["task", "rc.json.array=on"]
        if self.target:
            cmd.append(self.target)
        cmd.append("export")

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def add_task(self, task: TaskCIR, target: str) -> str:
        w = TaskWarrior()
        t = self.from_cif(task)
        tsk = w.task_add(**t)
        return tsk["uuid"]

    def delete_task(self, tool_uid: str) -> bool:
        """
        In Taskwarrior, we 'complete' tasks rather than hard-deleting them
        to keep the history in the 'Completed' list.
        """
        w = TaskWarrior()
        try:
            # Mark the task as done
            w.task_done(uuid=tool_uid)
            typer.echo(f"  ✅ Taskwarrior task {tool_uid[:8]} marked as DONE.")
            return True
        except Exception as e:
            # If already done or deleted, we treat as success
            if "not found" in str(e).lower():
                return True
            typer.secho(f"  ❌ Failed to complete Taskwarrior task: {e}", fg="red")
            return False

    def update_task(self, tool_uid: str, task: TaskCIR, target: str) -> str:

        w = TaskWarrior()
        if tool_uid is None:
            return self.add_task(task, target)

        # Fetch existing task
        try:
            _, tw_task = w.get_task(uuid=tool_uid)
            print(f"{tast=} {tw_task=}")
        except Exception:
            typer.secho(f"❌ Taskwarrior task {tool_uid} not found.", fg="red")
            return tool_uid

        # 1. Update Allowed Fields
        tw_task["description"] = task.description

        # Handle Target -> Project (e.g. project:uts -> uts)
        if target and target.startswith("project:"):
            tw_task["project"] = target.split(":", 1)[1]
        # Handle status

        # Handle body/notes (Taskwarrior uses 'annotations')
        if task.body:
            # Check if this note already exists to avoid duplicates
            existing_notes = [a["description"] for a in tw_task.get("annotations", [])]
            if task.body not in existing_notes:
                w.task_annotate(tool_uid, task.body)

        # 2. CRITICAL: Remove Read-Only Internal Fields
        # This prevents the "mask", "modified", and "entry" errors
        protected_fields = [
            "id",
            "mask",
            "urgency",
            "modified",
            "entry",
            "uuid",
            "status",  # status should be handled via w.task_done() if completed
        ]
        for field in protected_fields:
            tw_task.pop(field, None)

        # 3. Handle Status separately
        from universal_task_sync.models import TaskStatus

        if task.status == TaskStatus.COMPLETED:
            w.task_done(uuid=tool_uid)
        else:
            # Push updates for pending tasks
            print(f"at end {tast=} {tw_task=}")
            w.task_update(tw_task)

        return tool_uid

    def send_raw(self, raw_data: dict):
        """IO: Import into Taskwarrior."""
        # Taskwarrior import accepts a list of JSON objects via stdin
        input_json = json.dumps([raw_data])
        subprocess.run(["task", "import"], input=input_json, text=True, check=True)
