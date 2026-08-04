import os
from pathlib import Path

import hummingbot

# Overrides the auto-detected scripts directory. Set this to a Hummingbot source
# checkout's `scripts` folder (e.g. an editable dev environment) when the imported
# `hummingbot` package is a regular (non-editable) install and therefore has no
# sibling `scripts` directory of its own.
SCRIPTS_PATH_ENV_VAR = "HUMMINGBOT_SCRIPTS_PATH"


def get_hummingbot_scripts_path() -> Path:
    """
    Return the directory Hummingbot scripts are loaded from.

    Resolution order:
    1. The ``HUMMINGBOT_SCRIPTS_PATH`` environment variable, when set.
    2. The repository-level ``scripts`` directory sibling to the imported
       ``hummingbot`` package (only present for an editable/source checkout).
    """
    env_override = os.environ.get(SCRIPTS_PATH_ENV_VAR)
    if env_override:
        scripts_path = Path(env_override).expanduser().resolve()
        if not scripts_path.is_dir():
            raise FileNotFoundError(
                f"{SCRIPTS_PATH_ENV_VAR}='{env_override}' does not point to a directory"
            )
        return scripts_path

    package_file = getattr(hummingbot, "__file__", None)
    if not package_file:
        raise FileNotFoundError("Unable to locate the imported hummingbot package")

    scripts_path = Path(package_file).resolve().parent.parent / "scripts"
    if not scripts_path.is_dir():
        raise FileNotFoundError(
            "The imported hummingbot distribution does not expose its scripts directory. "
            f"Install hummingbot from a source checkout, or set the {SCRIPTS_PATH_ENV_VAR} "
            "environment variable to a Hummingbot scripts directory."
        )
    return scripts_path


def get_hummingbot_script_path(script_name: str) -> Path:
    """
    Resolve a script by name to its file on disk, searching subdirectories of the
    scripts folder (e.g. ``utility/``) so scripts don't need to live at the top level.

    ``script_name`` may be a bare stem ("v2_pmm_single_level"), in which case any
    matching file anywhere under the scripts directory is used, or a path relative
    to the scripts directory ("utility/v2_pmm_single_level") to disambiguate.
    """
    script_stem = script_name.removesuffix(".py").replace("\\", "/")
    path_parts = [part for part in script_stem.split("/") if part]
    if not path_parts or ".." in path_parts or any(part != Path(part).name for part in path_parts):
        raise FileNotFoundError(f"Invalid Hummingbot script name: '{script_name}'")

    scripts_path = get_hummingbot_scripts_path()

    direct_candidate = scripts_path.joinpath(*path_parts).with_suffix(".py")
    if direct_candidate.is_file():
        return direct_candidate

    matches = sorted(scripts_path.rglob(f"{path_parts[-1]}.py"))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        relative_matches = [str(match.relative_to(scripts_path).as_posix()) for match in matches]
        raise FileNotFoundError(
            f"Script '{script_name}' is ambiguous; matches found at: {relative_matches}"
        )

    raise FileNotFoundError(f"Script '{script_name}' not found in imported hummingbot scripts")
