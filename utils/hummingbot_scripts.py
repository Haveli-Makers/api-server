import importlib
from pathlib import Path

import hummingbot

from config import settings


def get_hummingbot_bundled_scripts_path() -> Path:
    """
    Return the ``hummingbot.scripts`` package directory.
    """
    try:
        scripts_module = importlib.import_module("hummingbot.scripts")
    except ModuleNotFoundError as exc:
        raise FileNotFoundError(
            "The imported hummingbot distribution does not expose a 'hummingbot.scripts' package."
        ) from exc

    scripts_path = Path(scripts_module.__file__).resolve().parent
    if not scripts_path.is_dir():
        raise FileNotFoundError("The 'hummingbot.scripts' package directory could not be located.")
    return scripts_path


def get_hummingbot_scripts_path() -> Path:
    """
    Return the repository-level scripts directory for the imported Hummingbot source.

    Hummingbot's scripts are siblings of the importable ``hummingbot`` package.
    They are available for an editable/source checkout, but are not package data
    in all installed distributions.
    """
    package_file = getattr(hummingbot, "__file__", None)
    if not package_file:
        raise FileNotFoundError("Unable to locate the imported hummingbot package")

    scripts_path = Path(package_file).resolve().parent.parent / "scripts"
    if not scripts_path.is_dir():
        raise FileNotFoundError(
            "The imported hummingbot distribution does not expose its scripts directory. "
            "Install hummingbot from a source checkout or editable installation."
        )
    return scripts_path


def get_community_scripts_path() -> Path:
    """
    Return the community scripts directory.
    """
    configured_path = settings.app.community_scripts_path
    if configured_path:
        return Path(configured_path).expanduser().resolve()
    return get_hummingbot_scripts_path() / "community"


def _validate_script_stem(script_name: str) -> str:
    script_stem = script_name.removesuffix(".py")
    if not script_stem or Path(script_stem).name != script_stem or ".." in script_stem:
        raise FileNotFoundError(f"Invalid Hummingbot script name: '{script_name}'")
    return script_stem


def get_hummingbot_script_path(script_name: str) -> Path:
    script_stem = _validate_script_stem(script_name)
    script_path = get_hummingbot_scripts_path() / f"{script_stem}.py"
    if not script_path.is_file():
        raise FileNotFoundError(f"Script '{script_name}' not found in imported hummingbot scripts")
    return script_path


def get_deployable_script_path(script_name: str) -> Path:
    """
    Locate a script to deploy.
    """
    script_stem = _validate_script_stem(script_name)
    search_dirs = [get_hummingbot_scripts_path(), get_community_scripts_path()]
    for directory in search_dirs:
        script_path = directory / f"{script_stem}.py"
        if script_path.is_file():
            return script_path
    raise FileNotFoundError(
        f"Script '{script_stem}.py' not found in: {', '.join(str(d) for d in search_dirs)}"
    )
