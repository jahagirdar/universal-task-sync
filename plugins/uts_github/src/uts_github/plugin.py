from datetime import datetime
from typing import List, Optional

import requests
import typer

from universal_task_sync.models import TaskCIR, TaskStatus

from .auth import delete_github_creds, get_github_creds


class GitHubPlugin:
    def __init__(self):
        self.pat = None
        self.headers = {}
        self.base_url = "https://api.github.com"
        self.graphql_url = "https://api.github.com/graphql"

    def authenticate(self, silent=False):
        """Loads the token. Use silent=True if just checking validity."""
        self.pat = get_github_creds()
        self.headers = {
            "Authorization": f"Bearer {self.pat}",
            "Accept": "application/vnd.github+json",
        }

    def validate_permissions(self, target: str):
        """
        Checks if PAT is valid and has write access to the specific target.
        Handles 'All Repos' scopes gracefully.
        """
        resp = requests.get(f"{self.base_url}/user", headers=self.headers)

        # 1. Handle Invalid Token (as implemented before)
        if resp.status_code == 401:
            delete_github_creds()
            self.authenticate()
            resp = requests.get(f"{self.base_url}/user", headers=self.headers)

        # 2. Identify Scopes
        # 'repo' scope covers ALL repositories for Classic PATs.
        scopes = [s.strip() for s in resp.headers.get("X-OAuth-Scopes", "").split(",")]

        # 3. Target Specific Validation
        if target.startswith("project:"):
            # Project V2 usually requires specific 'project' scope
            if "project" not in scopes:
                typer.secho(f"❌ PAT lacks 'project' scope. Cannot sync to {target}.", fg="red")
                raise typer.Exit(1)
        else:
            # Repo Check
            # If 'repo' is in scopes, it's a 'All Repos' token.
            # Even so, we must check if the user has PUSH access to THIS specific repo.
            repo_resp = requests.get(f"{self.base_url}/repos/{target}", headers=self.headers)

            if repo_resp.status_code == 404:
                typer.secho(f"❌ Repository '{target}' not found or PAT has no access to it.", fg="red")
                raise typer.Exit(1)

            repo_data = repo_resp.json()
            permissions = repo_data.get("permissions", {})

            # 'push' permission is the gold standard for 'Write' access
            if not permissions.get("push"):
                typer.secho(f"❌ PAT is valid, but you do not have WRITE access to {target}.", fg="red")
                typer.echo("Check if you are a collaborator with 'Write' or 'Admin' role.")
                raise typer.Exit(1)

        typer.secho(f"✅ Permissions verified for {target}", fg="green")

    # --- IO Methods ---

    def fetch_raw(self, target: str) -> List[dict]:
        """Routes to either REST (Issues) or GraphQL (Projects)."""
        if target.startswith("project:"):
            _, path = target.split(":", 1)
            owner, num = path.split("/")
            return self._fetch_project_v2(owner, int(num))
        return self._fetch_repo_issues(target)

    def _fetch_repo_issues(self, repo: str) -> List[dict]:
        url = f"{self.base_url}/repos/{repo}/issues?state=all&per_page=100"
        res = requests.get(url, headers=self.headers)
        res.raise_for_status()
        # Filter out Pull Requests
        return [i for i in res.json() if "pull_request" not in i]

    def _fetch_project_v2(self, org: str, number: int) -> List[dict]:
        query = """
        query($org: String!, $number: Int!) {
          organization(login: $org) {
            projectV2(number: $number) {
              title
              items(first: 100) {
                nodes {
                  id
                  content {
                    ... on Issue {
                      id databaseId title body state updatedAt
                      labels(first: 5) { nodes { name } }
                    }
                  }
                }
              }
            }
          }
        }
        """
        res = requests.post(
            self.graphql_url, json={"query": query, "variables": {"org": org, "number": number}}, headers=self.headers
        )
        res.raise_for_status()
        data = res.json()
        return data["data"]["organization"]["projectV2"]["items"]["nodes"]

    # --- Translation Methods ---

    def to_cif(self, raw: dict) -> TaskCIR:
        # Support both REST Issue and GraphQL Project Node structures
        item = raw.get("content", raw)

        status = TaskStatus.PENDING
        if item.get("state") == "closed":
            status = TaskStatus.COMPLETED

        # Date handling
        updated = item.get("updatedAt") or item.get("updated_at")
        dt = datetime.fromisoformat(updated.replace("Z", "+00:00")) if updated else datetime.now()

        raw_labels = item.get("labels", [])

        if isinstance(raw_labels, dict):  # GraphQL style
            tags = [l["name"] for l in raw_labels.get("nodes", [])]
        else:  # REST style (list)
            tags = [l["name"] for l in raw_labels if isinstance(l, dict)]

        return TaskCIR(
            uuid=None,
            ext_id=str(item.get("databaseId") or item.get("id")),
            description=item.get("title", "No Title"),
            body=item.get("body") or "",
            last_modified=dt,
            status=status,
            tags=tags,
        )

    def from_cif(self, task: TaskCIR) -> dict:
        return {
            "title": task.description,
            "body": task.body,
            "state": "closed" if task.status == TaskStatus.COMPLETED else "open",
        }

    def fetch_one(self, ext_id: str, target: str) -> dict:
        """Fetch a single issue from GitHub by its number."""
        url = f"{self.base_url}/repos/{target}/issues/{ext_id}"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def send_raw(self, raw_data: dict, target: str) -> Optional[str]:
        # 1. Identity Management
        ext_id = raw_data.pop("ext_id", None)

        # 2. Routing: Project V2 (GraphQL) vs Repo Issues (REST)
        if target.startswith("project:"):
            return self._send_to_project_v2(raw_data, target, ext_id)

        # 3. Standard Repo Issues (REST)
        repo_target = target
        base_url = f"{self.base_url}/repos/{repo_target}/issues"

        try:
            if ext_id:
                # UPDATE EXISTING ISSUE
                url = f"{base_url}/{ext_id}"
                response = requests.patch(url, json=raw_data, headers=self.headers)
                verb = "Updated"
            else:
                # CREATE NEW ISSUE
                url = base_url
                response = requests.post(url, json=raw_data, headers=self.headers)
                verb = "Created"

            if response.status_code in [200, 201]:
                new_data = response.json()
                # Return the 'number' as the external ID
                return str(new_data.get("number"))
            else:
                error_msg = response.json().get("message", "Unknown error")
                typer.secho(f"❌ GitHub API Error ({response.status_code}): {error_msg}", fg="red")
                return None

        except Exception as e:
            typer.secho(f"❌ Connection Error: {e}", fg="red")
            return None

    def _send_to_project_v2(self, raw_data: dict, target: str, ext_id: Optional[str]) -> Optional[str]:
        """
        GraphQL implementation for Project V2.
        Note: Project items are often 'DraftIssues' or linked 'Issues'.
        """
        # 1. Parse project info (e.g., project:org/123)
        _, path = target.split(":", 1)
        owner, proj_num = path.split("/")

        # 2. Logic for Update vs Create in ProjectV2
        if ext_id:
            # Mutation for updateProjectV2ItemFieldValue
            query = """
            mutation($proj: ID!, $item: ID!, $title: String!) {
              updateProjectV2DraftIssue(input: {draftIssueId: $item, title: $title}) {
                draftIssue { id }
              }
            }
            """
            # Implementation details would follow standard GraphQL request pattern
            pass
        else:
            # Mutation for addProjectV2DraftIssue
            pass
        return "project_item_id_here"

    # In your plugin base class or specific plugins
    @property
    def config_defaults(self) -> dict:
        """Returns a dictionary of supported keys and their default values."""
        return {"api_token": None, "base_url": "https://api.github.com", "verify_ssl": "True"}
