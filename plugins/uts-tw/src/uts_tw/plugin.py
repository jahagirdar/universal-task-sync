import json
import subprocess
from datetime import datetime, timedelta
from typing import List

import typer

from universal_task_sync.models import Priority, TaskCIR, TaskStatus


class TaskwarriorPlugin:
    """Strictly handles TW <-> CIF translation and IO."""

    def authenticate(self):
        pass

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
            ext_id=raw.get("uuid"),
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

        return {k: v for k, v in tw_dict.items() if v is not None}

    def fetch_raw(self, target: str) -> List[dict]:
        """IO: Export from Taskwarrior."""
        cmd = ["task", "rc.json.array=on"]
        if target:
            cmd.append(target)
        cmd.append("export")

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def update_task(self, ext_id: str, task: TaskCIR, target: str) -> str:
        from taskw import TaskWarrior

        w = TaskWarrior()

        # Fetch existing task
        try:
            _, tw_task = w.get_task(uuid=ext_id)
            print(f"{tast=} {tw_task=}")
        except Exception:
            typer.secho(f"❌ Taskwarrior task {ext_id} not found.", fg="red")
            return ext_id

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
                w.task_annotate(ext_id, task.body)

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
            w.task_done(uuid=ext_id)
        else:
            # Push updates for pending tasks
            print(f"at end {tast=} {tw_task=}")
            w.task_update(tw_task)

        return ext_id

    def send_raw(self, raw_data: dict):
        """IO: Import into Taskwarrior."""
        # Taskwarrior import accepts a list of JSON objects via stdin
        input_json = json.dumps([raw_data])
        subprocess.run(["task", "import"], input=input_json, text=True, check=True)
