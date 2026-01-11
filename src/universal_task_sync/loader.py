import importlib.metadata

import typer


def get_plugin(name: str):
    """
    Dynamically loads a plugin registered under the
    'universal_task_sync.plugins' entry point group.
    """
    # Find all entry points for our group
    eps = importlib.metadata.entry_points(group="universal_task_sync.plugins")

    # Try to find the one matching the requested name (e.g., 'tw' or 'github')
    plugin_entry = next((ep for ep in eps if ep.name == name), None)

    if not plugin_entry:
        typer.secho(f"❌ Plugin '{name}' not found.", fg=typer.colors.RED, err=True)
        installed = [ep.name for ep in eps]
        if installed:
            typer.echo(f"Available plugins: {', '.join(installed)}")
        else:
            typer.echo("No plugins installed. Did you run 'pip install -e .' in the plugin directories?")
        raise typer.Exit(code=1)

    try:
        # Load the class (e.g., TaskwarriorPlugin) and instantiate it
        plugin_class = plugin_entry.load()
        return plugin_class()
    except Exception as e:
        typer.secho(f"❌ Failed to load plugin '{name}': {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
