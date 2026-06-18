import importlib
import inspect
import json
import yaml
from typing import Any, Dict, List, Optional, Type

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from starlette import status

from deps import get_script_runner_service
from models import (
    Script,
    ScriptProcessRunRequest,
    ScriptRunRequest,
    ScriptRunResult,
    ScriptSchedule,
    ScriptScheduleCreate,
    ScriptScheduleHistory,
)
from services.script_runner import ScriptRunnerService
from utils.file_system import fs_util
from utils.hummingbot_scripts import get_hummingbot_script_path, get_hummingbot_scripts_path
from hummingbot.client.config.config_data_types import BaseClientModel
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


router = APIRouter(tags=["Scripts"], prefix="/scripts")


def _normalize_script_name(script_name: str) -> str:
    return script_name.removesuffix(".py").replace("-", "_")


def _load_script_module(script_name: str):
    normalized_script_name = _normalize_script_name(script_name)
    module_names = [
        f"hummingbot.scripts.{normalized_script_name}",
        f"bots.scripts.{normalized_script_name}",
    ]

    last_error = None
    for module_name in module_names:
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            last_error = exc

    raise HTTPException(
        status_code=404,
        detail=f"Script '{script_name}' not found ({last_error})",
    )


def _get_script_config_class(script_module) -> Optional[Type[BaseClientModel]]:
    for _, cls in inspect.getmembers(script_module, inspect.isclass):
        if cls.__module__ == script_module.__name__ and issubclass(cls, BaseClientModel) and cls is not BaseClientModel:
            return cls
    return None


def _get_script_strategy_class(script_module):
    strategy_classes = []
    for _, cls in inspect.getmembers(script_module, inspect.isclass):
        if cls.__module__ != script_module.__name__:
            continue
        if inspect.isclass(cls) and issubclass(cls, ScriptStrategyBase) and cls is not ScriptStrategyBase:
            strategy_classes.append(cls)

    if strategy_classes:
        return strategy_classes[0]

    for _, cls in inspect.getmembers(script_module, inspect.isclass):
        if cls.__module__ != script_module.__name__:
            continue
        if cls.__name__.lower().endswith("config"):
            continue
        if any(hasattr(cls, method_name) for method_name in ("fetch_and_store_spread", "run_once", "on_tick")):
            return cls

    return None


def _serialize_field_prompt(prompt: Any) -> Optional[str]:
    if prompt is None:
        return None
    if callable(prompt):
        try:
            return str(prompt(None))
        except TypeError:
            return None
    return str(prompt)


def _build_config_template(config_class: Type[BaseClientModel]) -> Dict[str, Dict[str, Any]]:
    template = {}
    required_fields = set()
    try:
        required_fields = set(config_class.model_json_schema().get("required", []))
    except Exception:
        required_fields = set()

    for field_name, field in config_class.model_fields.items():
        extra = field.json_schema_extra or {}
        default_factory = getattr(field, "default_factory", None)
        if callable(default_factory):
            default_value = default_factory()
        else:
            default_value = None if field.is_required() else field.default
        field_info = {
            "default": default_value,
            "required": field_name in required_fields,
            "annotation": str(field.annotation),
        }
        if field.description:
            field_info["description"] = field.description
        prompt = _serialize_field_prompt(extra.get("prompt"))
        if prompt:
            field_info["prompt"] = prompt
        template[field_name] = field_info

    return json.loads(json.dumps(template, default=str))


async def _run_script_once(script_instance):
    for method_name in ("run_once", "fetch_and_store_spread"):
        method = getattr(script_instance, method_name, None)
        if callable(method):
            result = method()
            if inspect.isawaitable(result):
                return await result
            return result

    raise HTTPException(
        status_code=400,
        detail="Script does not expose a supported one-shot run method",
    )

def _list_files_safe(directory: str) -> List[str]:
    try:
        return fs_util.list_files(directory)
    except FileNotFoundError:
        return []


@router.get("/", response_model=List[str])
async def list_scripts():
    """
    List scripts provided by the imported Hummingbot source checkout.
    
    Returns:
        List of script names (without .py extension)
    """
    return [f.replace('.py', '') for f in fs_util.list_files('scripts') if f.endswith('.py')]


@router.post("/runs/instant", response_model=ScriptRunResult)
async def run_script_instant(
    request: ScriptProcessRunRequest,
    script_runner: ScriptRunnerService = Depends(get_script_runner_service),
):
    """
    Run a strategy script immediately and return its output without storing history.
    """
    try:
        return await script_runner.run_instant(request)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/schedules/", response_model=ScriptSchedule, status_code=status.HTTP_201_CREATED)
async def create_script_schedule(
    request: ScriptScheduleCreate,
    script_runner: ScriptRunnerService = Depends(get_script_runner_service),
):
    """
    Create a recurring script schedule.
    """
    try:
        return await script_runner.create_schedule(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/schedules/", response_model=List[ScriptSchedule])
async def list_script_schedules(
    script_runner: ScriptRunnerService = Depends(get_script_runner_service),
):
    """
    List recurring script schedules.
    """
    return await script_runner.list_schedules()


@router.delete("/schedules/{schedule_id}")
async def delete_script_schedule(
    schedule_id: str,
    script_runner: ScriptRunnerService = Depends(get_script_runner_service),
):
    """
    Delete a recurring script schedule.
    """
    try:
        return await script_runner.delete_schedule(schedule_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")

@router.post(
    "/run",
    responses={
        400: {"description": "Invalid script run request or script initialization failure"},
        404: {"description": "Script, config class, or runnable class not found"},
        422: {"description": "Invalid script configuration"},
    },
)
async def run_script(
    request: ScriptRunRequest,
):
    script_module = _load_script_module(request.script_name)
    config_class = _get_script_config_class(script_module)
    if config_class is None:
        raise HTTPException(
            status_code=404,
            detail=f"Script configuration class for '{request.script_name}' not found",
        )

    config_template = _build_config_template(config_class)
    if not request.config:
        return {
            "status": "requires_config",
            "script_name": _normalize_script_name(request.script_name),
            "config": config_template,
        }

    try:
        config = config_class(**request.config)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Invalid script config",
                "errors": exc.errors(),
                "config": config_template,
            },
        )
    strategy_class = _get_script_strategy_class(script_module)
    if strategy_class is None:
        raise HTTPException(
            status_code=404,
            detail=f"Runnable script class for '{request.script_name}' not found",
        )

    normalized_script_name = _normalize_script_name(request.script_name)
    script = strategy_class(connectors={}, config=config)
    result = await _run_script_once(script)

    return {
        "status": "success",
        "script_name": normalized_script_name,
        "config": config.model_dump(),
        "result": result,
    }

@router.post("/schedules/{schedule_id}/run", response_model=ScriptRunResult)
async def run_script_schedule_now(
    schedule_id: str,
    script_runner: ScriptRunnerService = Depends(get_script_runner_service),
):
    """
    Trigger a scheduled script immediately and store the output in its history.
    """
    try:
        return await script_runner.run_schedule_now(schedule_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/schedules/{schedule_id}/history", response_model=ScriptScheduleHistory)
async def get_script_schedule_history(
    schedule_id: str,
    limit: int = Query(default=50, ge=1, le=50),
    script_runner: ScriptRunnerService = Depends(get_script_runner_service),
):
    """
    Return up to the latest 50 outputs for a scheduled script.
    """
    schedules = await script_runner.list_schedules()
    if not any(schedule.id == schedule_id for schedule in schedules):
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    return ScriptScheduleHistory(schedule_id=schedule_id, runs=await script_runner.get_history(schedule_id, limit))


# Script Configuration endpoints (must come before script name routes)
@router.get("/configs/", response_model=List[Dict])
async def list_script_configs():
    """
    List all script configurations with metadata.
    
    Returns:
        List of script configuration objects with name, script_file_name, and other metadata
    """
    try:
        config_files = [
            *[("conf/scripts", f) for f in _list_files_safe("conf/scripts") if f.endswith((".yml", ".json"))],
            *[("conf", f) for f in _list_files_safe("conf") if f.endswith((".yml", ".json"))],
        ]
        configs = []
        
        for config_directory, config_file in config_files:
            config_name = config_file.rsplit(".", 1)[0]
            try:
                if config_file.endswith(".json"):
                    config = json.loads(fs_util.read_file(f"{config_directory}/{config_file}"))
                else:
                    config = fs_util.read_yaml_file(f"{config_directory}/{config_file}")
                configs.append({
                    "config_name": config_name,
                    "script_file_name": config.get("script_file_name", "unknown"),
                    "controllers_config": config.get("controllers_config", []),
                    "candles_config": config.get("candles_config", []),
                    "markets": config.get("markets", {})
                })
            except Exception as e:
                # If config is malformed, still include it with basic info
                configs.append({
                    "config_name": config_name,
                    "script_file_name": "error",
                    "error": str(e)
                })
        
        return configs
    except FileNotFoundError:
        return []


@router.get("/configs/{config_name}", response_model=Dict)
async def get_script_config(config_name: str):
    """
    Get script configuration by config name.
    
    Args:
        config_name: Name of the configuration file to retrieve
        
    Returns:
        Dictionary with script configuration
        
    Raises:
        HTTPException: 404 if configuration not found
    """
    try:
        config = fs_util.read_yaml_file(f"conf/scripts/{config_name}.yml")
        return config
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Configuration '{config_name}' not found")


@router.post("/configs/{config_name}", status_code=status.HTTP_201_CREATED)
async def create_or_update_script_config(config_name: str, config: Dict):
    """
    Create or update script configuration.
    
    Args:
        config_name: Name of the configuration file
        config: Configuration dictionary to save
        
    Returns:
        Success message when configuration is saved
        
    Raises:
        HTTPException: 400 if save error occurs
    """
    try:
        yaml_content = yaml.dump(config, default_flow_style=False)
        fs_util.add_file('conf/scripts', f"{config_name}.yml", yaml_content, override=True)
        return {"message": f"Configuration '{config_name}' saved successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/configs/{config_name}")
async def delete_script_config(config_name: str):
    """
    Delete script configuration.
    
    Args:
        config_name: Name of the configuration file to delete
        
    Returns:
        Success message when configuration is deleted
        
    Raises:
        HTTPException: 404 if configuration not found
    """
    try:
        fs_util.delete_file('conf/scripts', f"{config_name}.yml")
        return {"message": f"Configuration '{config_name}' deleted successfully"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Configuration '{config_name}' not found")


@router.get("/{script_name}", response_model=Dict[str, str])
async def get_script(script_name: str):
    """
    Get script content by name.
    
    Args:
        script_name: Name of the script to retrieve
        
    Returns:
        Dictionary with script name and content
        
    Raises:
        HTTPException: 404 if script not found
    """
    try:
        content = get_hummingbot_script_path(script_name).read_text(encoding="utf-8")
        return {
            "name": script_name,
            "content": content
        }
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Script '{script_name}' not found")


@router.post("/{script_name}", status_code=status.HTTP_201_CREATED)
async def create_or_update_script(script_name: str, script: Script):
    """
    Imported Hummingbot scripts are dependency-owned and cannot be edited here.
    """
    raise HTTPException(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        detail="Scripts are provided by the imported hummingbot installation and are read-only",
    )


@router.delete("/{script_name}")
async def delete_script(script_name: str):
    """
    Imported Hummingbot scripts are dependency-owned and cannot be deleted here.
    """
    raise HTTPException(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        detail="Scripts are provided by the imported hummingbot installation and are read-only",
    )


@router.get("/{script_name}/config/template", response_model=Dict)
async def get_script_config_template(script_name: str):
    """
    Get script configuration template with default values.
    
    Args:
        script_name: Name of the script to get template for
        
    Returns:
        Dictionary with configuration template and default values
        
    Raises:
        HTTPException: 404 if script configuration class not found
    """
    try:
        config_class = fs_util.load_script_config_class(
            script_name,
            script_path=get_hummingbot_script_path(script_name),
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Script '{script_name}' not found")
    if config_class is None:
        raise HTTPException(status_code=404, detail=f"Script configuration class for '{script_name}' not found")

    # Extract fields and default values
    config_fields = {name: field.default for name, field in config_class.model_fields.items()}
    return json.loads(json.dumps(config_fields, default=str))
