import logging
from datetime import datetime
from typing import Dict, List, Optional

import requests
import typer

from universal_task_sync.base import BasePlugin
from universal_task_sync.models import TaskCIR, TaskStatus

from .auth import delete_github_creds, get_github_creds

logger = logging.getLogger(__name__)


class GitHubPlugin(BasePlugin):
    """
    Full-fidelity GitHub plugin:
    - Issues = canonical tasks
    - Project V2 = planning metadata
    - GraphQL issue hierarchy = dependencies
    """

    # -------------------------
    # Lifecycle
    # -------------------------

    def __init__(self):
        self.base_url = "https://api.github.com"
        self.graphql_url = "https://api.github.com/graphql"
        self.headers: Dict[str, str] = {}
        self.target: Optional[str] = None
        self.project_id: Optional[str] = None
        self.project_fields: Dict[str, str] = {}

    @property
    def name(self) -> str:
        return "github"

    def set_filter(self, target: str) -> None:
        self.target = target

    # -------------------------
    # Auth
    # -------------------------

    def authenticate(self) -> bool:
        token = get_github_creds()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

        r = requests.get(f"{self.base_url}/user", headers=self.headers)
        if r.status_code == 401:
            delete_github_creds()
            token = get_github_creds(force_prompt=True)
            self.headers["Authorization"] = f"Bearer {token}"
            r = requests.get(f"{self.base_url}/user", headers=self.headers)

        return r.status_code == 200

    def validate_permissions(self) -> None:
        self._require_target()
        resp = requests.get(f"{self.base_url}/repos/{self.target}", headers=self.headers)
        if resp.status_code != 200:
            typer.secho(f"❌ No access to repository {self.target}", fg="red")
            raise typer.Exit(1)

        perms = resp.json().get("permissions", {})
        if not perms.get("push"):
            typer.secho(f"❌ No write access to {self.target}", fg="red")
            raise typer.Exit(1)

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch_raw(self) -> List[dict]:
        self._require_target()
        return self._fetch_issues_with_hierarchy(self.target)

    def _fetch_issues_with_hierarchy(self, repo: str) -> List[dict]:
        """
        GraphQL fetch:
        - issues
        - parent / subIssues
        """
        query = """
        query($owner:String!, $repo:String!) {
          repository(owner:$owner, name:$repo) {
            issues(first:100, states:[OPEN,CLOSED]) {
              nodes {
                id number title body state updatedAt
                labels(first:20){ nodes{ name } }
                parent { id number }
                subIssues(first:50){ nodes{ id number } }
              }
            }
          }
        }
        """
        owner, name = repo.split("/")
        res = requests.post(
            self.graphql_url,
            headers=self.headers,
            json={"query": query, "variables": {"owner": owner, "repo": name}},
        )
        res.raise_for_status()
        return res.json()["data"]["repository"]["issues"]["nodes"]

    # -------------------------
    # Translation
    # -------------------------

    def to_cif(self, raw: dict) -> TaskCIR:
        tool_uid = f"issue:{raw['number']}"
        logger.debug(f"{raw=} {raw['number']=} {raw['labels']=}")
        if len(raw["labels"]) > 0:
            tags = [l["name"] for l in raw["labels"]["nodes"]]
        else:
            tags = []

        status = TaskStatus.COMPLETED if raw["state"] == "CLOSED" else TaskStatus.PENDING

        depends = [f"issue:{raw['parent']['number']}"] if raw.get("parent") else []
        followers = [f"issue:{i['number']}" for i in raw.get("subIssues", {}).get("nodes", [])]

        return TaskCIR(
            uuid="",
            tool_uid=tool_uid,
            description=raw["title"],
            body=raw.get("body") or "",
            status=status,
            tags=tags,
            depends=depends,
            followers=followers,
            last_modified=datetime.fromisoformat(raw["updatedAt"].replace("Z", "+00:00")),
            custom_fields={"_github": {"issue_node_id": raw["id"]}},
        )

    def from_cif(self, task: TaskCIR) -> dict:
        data = {
            "title": task.description,
            "body": task.body,
            "state": "closed" if task.status == TaskStatus.COMPLETED else "open",
            "labels": task.tags,
        }

        if task.priority:
            data["labels"].append(f"priority:{task.priority.value}")

        return data

    # -------------------------
    # Write Issue
    # -------------------------

    def update_task(self, tool_uid: str, task: TaskCIR, target: str) -> str:
        number = tool_uid.split(":")[1]
        url = f"{self.base_url}/repos/{target}/issues/{number}"
        r = requests.patch(url, headers=self.headers, json=self.from_cif(task))
        r.raise_for_status()
        return tool_uid

    def send_raw(self, raw_item: dict, target: str) -> str:
        url = f"{self.base_url}/repos/{target}/issues"
        r = requests.post(url, headers=self.headers, json=raw_item)
        r.raise_for_status()
        return f"issue:{r.json()['number']}"

    # -------------------------
    # Dependencies (Sub-Issues)
    # -------------------------

    def update_relationships(self, tool_uid: str, task: TaskCIR, mgr):
        issue_node_id = task.custom_fields["_github"]["issue_node_id"]

        for dep in task.depends:
            parent_node = self._resolve_node_id(dep, mgr)
            self._set_parent(parent_node, issue_node_id)

    def _set_parent(self, parent_id: str, child_id: str):
        mutation = """
        mutation($parent:ID!, $child:ID!) {
          addSubIssue(input:{ issueId:$parent, subIssueId:$child }) {
            issue { id }
          }
        }
        """
        requests.post(
            self.graphql_url,
            headers=self.headers,
            json={"query": mutation, "variables": {"parent": parent_id, "child": child_id}},
        )

    # -------------------------
    # Helpers
    # -------------------------

    def _resolve_node_id(self, tool_uid: str, mgr) -> str:
        # Fetch from DB snapshot or refetch
        raise NotImplementedError("node lookup cache omitted for brevity")

    @property
    def config_defaults(self) -> dict:
        return {}

    def fetch_one(self, tool_uid: str) -> dict:
        """
        Fetch a single issue by number.
        Uses the bound target (repo).
        """
        self._require_target()
        number = tool_uid.split(":")[1]
        url = f"{self.base_url}/repos/{self.target}/issues/{number}"
        r = requests.get(url, headers=self.headers)
        r.raise_for_status()
        return r.json()

    def patch_raw(self, tool_id: str, raw_item: dict) -> bool:
        """
        Patch an existing issue.
        """
        self._require_target()
        number = tool_id.split(":")[1]
        url = f"{self.base_url}/repos/{self.target}/issues/{number}"

        r = requests.patch(url, headers=self.headers, json=raw_item)
        r.raise_for_status()
        return True

    def delete_task(self, tool_uid: str) -> bool:
        """
        GitHub does not allow deletion.
        We treat delete as close.
        """
        self._require_target()
        number = tool_uid.split(":")[1]
        url = f"{self.base_url}/repos/{self.target}/issues/{number}"

        r = requests.patch(
            url,
            headers=self.headers,
            json={"state": "closed"},
        )
        r.raise_for_status()
        return True

    @property
    def capabilities(self):
        return {
            "delete": "soft",
            "dependencies": "native",
            "projects": "v2",
            "custom_fields": "project-scoped",
        }

    def _require_target(self):
        if not self.target:
            raise RuntimeError("Plugin target not set via set_filter()")
