"""
factory.config
==============
Configuration loading and settings for the AI Software Factory.

Loads factory.yaml, models.yaml, and policies.yaml from the configs/
directory. Project-specific overrides may extend (but not weaken)
non-negotiable safety policies.

Usage::

    from factory.config import get_settings
    settings = get_settings()
    print(settings.factory.version)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Directory constants (still needed by other modules)
# ---------------------------------------------------------------------------

FACTORY_ROOT: Path = Path(__file__).resolve().parent.parent
CONFIG_DIR: Path = FACTORY_ROOT / "configs"

PROJECTS_DIR: Path = FACTORY_ROOT / "projects"
ARTIFACTS_DIR: Path = FACTORY_ROOT / "artifacts"
LOGS_DIR: Path = FACTORY_ROOT / "logs"
KNOWLEDGE_DIR: Path = FACTORY_ROOT / "knowledge"
CACHE_DIR: Path = FACTORY_ROOT / "cache"
BACKUPS_DIR: Path = FACTORY_ROOT / "backups"
TEMP_DIR: Path = FACTORY_ROOT / "temp"
MODELS_DIR: Path = FACTORY_ROOT / "models"
DB_PATH: Path = FACTORY_ROOT / "factory.db"

# ---------------------------------------------------------------------------
# Settings dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FactorySettings:
    """Top-level factory configuration."""
    version: str = "0.1.0"
    name: str = "AI Software Factory"
    default_profile: str = "balanced"
    max_concurrent_tasks: int = 2
    emergency_stop_file: str = ".factory_stop"
    log_level: str = "INFO"
    # Whether to allow the factory to auto-approve inherited-scope actions
    auto_approve_inherited: bool = True


@dataclass
class OllamaSettings:
    """Ollama provider configuration."""
    base_url: str = "http://localhost:11434"
    request_timeout_seconds: int = 120
    model_pull_enabled: bool = False  # do NOT auto-pull models


@dataclass
class OpenCodeSettings:
    """OpenCode remote provider configuration."""
    enabled: bool = False
    base_url: str = ""
    # API key is read from env, NEVER stored here
    api_key_env_var: str = "OPENCODE_API_KEY"
    allow_sensitive_data: bool = False  # must be explicitly unlocked


@dataclass
class OpenCodeRuntimeSettings:
    """
    OpenCode server lifecycle configuration.
    Kept in factory.yaml (runtime) — separate from models.yaml (capabilities).
    """
    host: str = "127.0.0.1"
    port: int = 7878
    startup_timeout_seconds: int = 10
    request_timeout_seconds: int = 120
    # If True, factory will start and manage its own opencode serve process
    manage_server: bool = True


@dataclass
class ModelsSettings:
    """Model registry configuration."""
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    opencode: OpenCodeSettings = field(default_factory=OpenCodeSettings)
    # Default model used when no agent-specific override is given.
    # Format: "<provider>:<model>"  e.g. "ollama:qwen2.5-coder:7b"
    default_model: str = "ollama:qwen2.5-coder:7b"
    # Fallback when preferred model unavailable
    fallback_to_stub: bool = True


@dataclass
class ResourceSettings:
    """Hardware resource budget."""
    total_ram_gb: float = 16.0
    total_vram_gb: float = 6.0
    # Maximum RAM reserved for concurrent AI model inference
    max_inference_ram_gb: float = 10.0
    max_inference_vram_gb: float = 5.5


@dataclass
class PolicySettings:
    """Non-negotiable safety policy settings."""
    # Git mutations always require human approval
    require_approval_git_mutations: bool = True
    # Sensitive file access always requires human approval
    require_approval_sensitive_data: bool = True
    # High-risk and critical actions always require human approval
    require_approval_high_risk: bool = True
    # Remote models cannot receive sensitive data unless explicitly allowed
    block_sensitive_to_remote: bool = True
    # Knowledge decisions at high impact require human approval
    require_approval_knowledge_high_impact: bool = True
    # Bounded retries for failed tasks
    max_task_retries: int = 3
    # Production deployment is always outside factory authority
    production_deployment_forbidden: bool = True


@dataclass
class FullSettings:
    """Composite settings object returned by get_settings()."""
    factory: FactorySettings = field(default_factory=FactorySettings)
    models: ModelsSettings = field(default_factory=ModelsSettings)
    resources: ResourceSettings = field(default_factory=ResourceSettings)
    policies: PolicySettings = field(default_factory=PolicySettings)
    opencode_runtime: OpenCodeRuntimeSettings = field(
        default_factory=OpenCodeRuntimeSettings
    )


# ---------------------------------------------------------------------------
# YAML loading helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning an empty dict if missing or empty."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError as exc:
        from factory.errors import ConfigurationError
        raise ConfigurationError(
            f"Failed to parse {path}: {exc}"
        ) from exc


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_factory_yaml(settings: FullSettings, data: dict[str, Any]) -> None:
    """Apply factory.yaml values onto the FullSettings instance."""
    f = data.get("factory", {})
    fs = settings.factory
    if "version" in f:
        fs.version = str(f["version"])
    if "name" in f:
        fs.name = str(f["name"])
    if "default_profile" in f:
        fs.default_profile = str(f["default_profile"])
    if "max_concurrent_tasks" in f:
        fs.max_concurrent_tasks = int(f["max_concurrent_tasks"])
    if "log_level" in f:
        fs.log_level = str(f["log_level"])
    if "auto_approve_inherited" in f:
        fs.auto_approve_inherited = bool(f["auto_approve_inherited"])

    r = data.get("resources", {})
    rs = settings.resources
    if "total_ram_gb" in r:
        rs.total_ram_gb = float(r["total_ram_gb"])
    if "total_vram_gb" in r:
        rs.total_vram_gb = float(r["total_vram_gb"])
    if "max_inference_ram_gb" in r:
        rs.max_inference_ram_gb = float(r["max_inference_ram_gb"])
    if "max_inference_vram_gb" in r:
        rs.max_inference_vram_gb = float(r["max_inference_vram_gb"])

    # OpenCode runtime (server lifecycle) — separate from models.yaml
    ocr = data.get("opencode_runtime", {})
    ort = settings.opencode_runtime
    if "host" in ocr:
        ort.host = str(ocr["host"])
    if "port" in ocr:
        ort.port = int(ocr["port"])
    if "startup_timeout_seconds" in ocr:
        ort.startup_timeout_seconds = int(ocr["startup_timeout_seconds"])
    if "request_timeout_seconds" in ocr:
        ort.request_timeout_seconds = int(ocr["request_timeout_seconds"])
    if "manage_server" in ocr:
        ort.manage_server = bool(ocr["manage_server"])


def _apply_models_yaml(settings: FullSettings, data: dict[str, Any]) -> None:
    """Apply models.yaml values onto the FullSettings instance."""
    o = data.get("ollama", {})
    os_ = settings.models.ollama
    if "base_url" in o:
        os_.base_url = str(o["base_url"])
    if "request_timeout_seconds" in o:
        os_.request_timeout_seconds = int(o["request_timeout_seconds"])
    if "model_pull_enabled" in o:
        os_.model_pull_enabled = bool(o["model_pull_enabled"])

    oc = data.get("opencode", {})
    ocs = settings.models.opencode
    if "enabled" in oc:
        ocs.enabled = bool(oc["enabled"])
    if "base_url" in oc:
        ocs.base_url = str(oc["base_url"])
    if "api_key_env_var" in oc:
        ocs.api_key_env_var = str(oc["api_key_env_var"])
    if "allow_sensitive_data" in oc:
        ocs.allow_sensitive_data = bool(oc["allow_sensitive_data"])

    # Default model — stays in models.yaml (capability config, not runtime)
    if "default_model" in data:
        settings.models.default_model = str(data["default_model"])


def _apply_policies_yaml(settings: FullSettings, data: dict[str, Any]) -> None:
    """
    Apply policies.yaml values onto the FullSettings instance.
    Non-negotiable policies (prefixed 'require_approval_*' and
    'production_deployment_forbidden') cannot be weakened.
    """
    ps = settings.policies
    _NON_NEGOTIABLE = {
        "require_approval_git_mutations",
        "require_approval_sensitive_data",
        "require_approval_high_risk",
        "block_sensitive_to_remote",
        "production_deployment_forbidden",
    }
    p = data.get("policies", {})
    for key, value in p.items():
        if not hasattr(ps, key):
            continue
        current = getattr(ps, key)
        # Non-negotiable bool policies cannot be set to False
        if key in _NON_NEGOTIABLE and isinstance(current, bool) and current:
            if not bool(value):
                # Silently ignore attempts to weaken — log this
                import warnings
                warnings.warn(
                    f"Policy '{key}' is non-negotiable and cannot be disabled.",
                    stacklevel=2,
                )
                continue
        setattr(ps, key, value)


@lru_cache(maxsize=1)
def get_settings() -> FullSettings:
    """
    Load and return the singleton FullSettings for this factory instance.

    Loads (in order):
      configs/factory.yaml   — factory + resources + opencode_runtime
      configs/models.yaml    — ollama + opencode credentials + default_model
      configs/policies.yaml  — safety policies

    Environment variable ``FACTORY_CONFIG_DIR`` overrides the config directory.
    """
    config_dir = Path(
        os.environ.get("FACTORY_CONFIG_DIR", str(CONFIG_DIR))
    )

    settings = FullSettings()

    factory_data = _load_yaml(config_dir / "factory.yaml")
    models_data = _load_yaml(config_dir / "models.yaml")
    policies_data = _load_yaml(config_dir / "policies.yaml")

    _apply_factory_yaml(settings, factory_data)
    _apply_models_yaml(settings, models_data)
    _apply_policies_yaml(settings, policies_data)

    return settings


def reload_settings() -> FullSettings:
    """Force reload of settings (clears LRU cache). Useful in tests."""
    get_settings.cache_clear()
    return get_settings()
