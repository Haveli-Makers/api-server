from pathlib import Path

import hummingbot


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


def get_hummingbot_script_path(script_name: str) -> Path:
    script_stem = script_name.removesuffix(".py")
    if not script_stem or Path(script_stem).name != script_stem or ".." in script_stem:
        raise FileNotFoundError(f"Invalid Hummingbot script name: '{script_name}'")

    script_path = get_hummingbot_scripts_path() / f"{script_stem}.py"
    if not script_path.is_file():
        raise FileNotFoundError(f"Script '{script_name}' not found in imported hummingbot scripts")
    return script_path
