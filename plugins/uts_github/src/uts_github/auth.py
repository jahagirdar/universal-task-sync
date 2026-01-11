import json
import os
from pathlib import Path

import typer


def get_config_path() -> Path:
    return Path.home() / ".config" / "universal_task_sync" / "github.json"


def get_github_creds(force_prompt: bool = False) -> str:
    config_path = get_config_path()

    # If forced or file doesn't exist, prompt user
    if force_prompt or not config_path.exists():
        if force_prompt:
            typer.secho("❌ The stored GitHub PAT is invalid or expired.", fg="red")

        typer.secho("🔑 GitHub Authentication Required", fg="magenta", bold=True)
        pat = typer.prompt("Enter a valid GitHub Personal Access Token", hide_input=True)

        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump({"pat": pat}, f)

    with open(config_path) as f:
        return json.load(f).get("pat")


def delete_github_creds():
    config_path = get_config_path()
    if config_path.exists():
        os.remove(config_path)
