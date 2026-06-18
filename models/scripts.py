from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator


# Script file operations
class Script(BaseModel):
    """Script file content"""
    content: str = Field(description="Script source code")


class ScriptResponse(BaseModel):
    """Response for getting a script"""
    name: str = Field(description="Script name")
    content: str = Field(description="Script source code")


# Script configuration operations
class ScriptConfig(BaseModel):
    """Script configuration content"""
    config_name: str = Field(description="Configuration name")
    script_file_name: str = Field(description="Script file name")
    controllers_config: List[str] = Field(default=[], description="List of controller configurations")
    candles_config: List[Dict[str, Any]] = Field(default=[], description="Candles configuration")
    markets: Dict[str, Any] = Field(default={}, description="Markets configuration")


class ScriptConfigResponse(BaseModel):
    """Response for script configuration with metadata"""
    config_name: str = Field(description="Configuration name")
    script_file_name: str = Field(description="Script file name")
    controllers_config: List[str] = Field(default=[], description="List of controller configurations")
    candles_config: List[Dict[str, Any]] = Field(default=[], description="Candles configuration")
    markets: Dict[str, Any] = Field(default={}, description="Markets configuration")
    error: Optional[str] = Field(None, description="Error message if config is malformed")


class ScriptRunRequest(BaseModel):
    """Request body to run a script once with dynamic configuration"""
    script_name: str = Field(description="Script name to run, without .py extension")
    config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Script configuration parameters. Send an empty object to receive the script config schema.",
    )


class ScriptProcessRunRequest(BaseModel):
    """Request to run a script through the scheduler runner."""

    strategy_name: str = Field(description="Strategy script name without .py extension")
    config_name: Optional[str] = Field(default=None, description="Optional configuration file name without extension")
    account_name: Optional[str] = Field(default=None, description="Account to pass to the script runner")
    verbose: bool = Field(default=False, description="Enable verbose script output")
    extra_args: Union[str, List[str]] = Field(
        default="",
        description="CLI arguments appended to the script command as a string or list",
    )

    @field_validator("extra_args", mode="before")
    @classmethod
    def normalize_extra_args(cls, value):
        if value is None:
            return ""
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        return value

class ScriptRunResult(BaseModel):
    """Result of a script run."""

    run_id: str = Field(description="Unique run identifier")
    strategy_name: str
    config_name: Optional[str] = None
    account_name: Optional[str] = None
    started_at: datetime
    completed_at: datetime
    status: Literal["success", "failed"]
    output: str = Field(description="Combined stdout and stderr from the script run")
    return_code: Optional[int] = None


class ScriptScheduleCreate(BaseModel):
    """Request to create a scheduled script run."""

    name: str = Field(description="Human readable scheduled task name")
    strategy_name: str = Field(description="Strategy script name without .py extension")
    config_name: Optional[str] = Field(default=None, description="Optional configuration file name without extension")
    account_name: Optional[str] = Field(default=None, description="Account to pass to the script runner")
    interval_value: int = Field(gt=0, description="Positive interval value")
    interval_unit: Literal["seconds", "minutes", "hours", "weeks"] = Field(description="Interval unit")
    verbose: bool = Field(default=False, description="Enable verbose script output")
    extra_args: Union[str, List[str]] = Field(
        default="",
        description="Raw CLI argument fragment appended to the script command",
    )

    @field_validator("extra_args", mode="before")
    @classmethod
    def normalize_extra_args(cls, value):
        if value is None:
            return ""
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        return value


class ScriptSchedule(ScriptScheduleCreate):
    """Scheduled script metadata."""

    id: str
    created_at: datetime
    next_run_at: datetime
    last_run_at: Optional[datetime] = None
    enabled: bool = True


class ScriptScheduleHistory(BaseModel):
    """History for a scheduled script."""

    schedule_id: str
    runs: List[ScriptRunResult]
