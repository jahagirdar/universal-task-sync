import os
import sys
from pathlib import Path

import yaml

if sys.version_info < (3, 10):
    from importlib_metadata import entry_points
else:
    from importlib.metadata import entry_points

CORE_DEFAULTS = {
    "difftool": "vimdiff",
    "sync_interval": "300",
}


# ... (keep your existing imports and UniversalConfig class) ...


def get_config() -> dict:
    """
    Returns the final configuration dictionary:
    Default Manifest + User Overrides from settings.yaml
    """
    cfg_manager = UniversalConfig()
    manifest = get_full_manifest()

    if cfg_manager.config_file.exists():
        try:
            with open(cfg_manager.config_file) as f:
                user_settings = yaml.safe_load(f) or {}
                # Update defaults with user settings
                manifest.update(user_settings)
        except Exception as e:
            print(f"⚠️ Error loading {cfg_manager.config_file}: {e}")

    # Add the paths to the config so they are accessible everywhere
    manifest["_paths"] = {
        "config_file": cfg_manager.config_file,
        "state_db": cfg_manager.state_db,
        "config_home": cfg_manager.config_home,
        "data_home": cfg_manager.data_home,
    }

    return manifest


def get_full_manifest() -> dict:
    """
    Discovery loop:
    1. Start with Core defaults.
    2. Find all installed UTS plugins via Entry Points.
    3. Load each plugin and merge its 'config_defaults' into the manifest.
    """
    manifest = CORE_DEFAULTS.copy()

    # 'uts.plugins' should match the group in your setup.py / pyproject.toml
    plugins = entry_points(group="uts.plugins")

    for ep in plugins:
        try:
            plugin_class = ep.load()
            # Instantiate or use a class property to get defaults
            plugin_instance = plugin_class()

            # Namespace keys by plugin name (e.g., github.api_token)
            prefix = plugin_instance.name
            for key, val in plugin_instance.config_defaults.items():
                manifest[f"{prefix}.{key}"] = val
        except Exception as e:
            # Don't let one broken plugin crash the config tool
            print(f"⚠️ Failed to load manifest for plugin {ep.name}: {e}")

    return manifest


class UniversalConfig:
    APP_NAME = "universal_task_sync"

    def __init__(self):
        # XDG Compliance
        self.config_home = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / self.APP_NAME
        self.data_home = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local/share")) / self.APP_NAME

        # Paths
        self.config_file = self.config_home / "settings.yaml"
        self.state_db = self.data_home / "map.db"

        # Ensure existence
        self.config_home.mkdir(parents=True, exist_ok=True)
        self.data_home.mkdir(parents=True, exist_ok=True)
