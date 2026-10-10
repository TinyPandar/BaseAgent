"""Declarative host defaults. CLI overrides config; config never imports code."""

from pathlib import Path
import math

from baseagent.capabilities.assembly import BUILTIN_PACKAGES
from baseagent.jsondata import read_json_object


SECTIONS = {
    "workspace": {"root", "db"},
    "capabilities": {"tool_packages", "skills_dirs"},
    "permissions": {"allow_write", "allow_command", "allow_memory_publish"},
    "run": {"model", "max_steps", "max_model_calls", "max_tool_calls", "max_context_bytes",
            "max_tool_context_bytes", "max_total_tokens", "max_duration_seconds", "model_retries",
            "tool_retries", "trace", "estimate_model", "preauthorize_model", "max_completion_tokens",
            "tokenizer_file", "estimate_margin_percent", "estimate_fixed_margin", "request_bound_profile"},
    "policies": {"tool_policy", "completion_policy", "subtasks_config"},
}
PATH_FIELDS = {"root", "db", "tokenizer_file", "tool_policy", "completion_policy", "subtasks_config"}
BOOL_FIELDS = SECTIONS["permissions"] | {"trace", "estimate_model", "preauthorize_model"}
INT_MIN = {"max_steps": 1, "max_model_calls": 1, "max_tool_calls": 0,
           "max_context_bytes": 256, "max_tool_context_bytes": 256, "max_total_tokens": 1,
           "model_retries": 0, "tool_retries": 0, "max_completion_tokens": 1,
           "estimate_margin_percent": 0, "estimate_fixed_margin": 0}


def load_harness_config(path):
    """Return validated argparse defaults, resolving paths against the file."""
    path = Path(path).resolve()
    data = read_json_object(path, max_bytes=32_000, label="harness configuration")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("harness configuration requires version: 1")
    unknown = set(data) - {"version", *SECTIONS}
    if unknown:
        raise ValueError("unknown harness configuration sections: " + ", ".join(sorted(unknown)))
    values = {}
    for section, fields in SECTIONS.items():
        settings = data.get(section, {})
        if not isinstance(settings, dict) or set(settings) - fields:
            raise ValueError(f"invalid or unknown fields in configuration section {section}")
        values.update(settings)
    for key, value in values.items():
        if key in BOOL_FIELDS:
            if type(value) is not bool:
                raise ValueError(f"{key} must be boolean")
        elif key in INT_MIN:
            if type(value) is not int or value < INT_MIN[key]:
                raise ValueError(f"{key} must be an integer >= {INT_MIN[key]}")
            if key in {"model_retries", "tool_retries"} and value > 4:
                raise ValueError(f"{key} must be 0-4")
        elif key == "max_duration_seconds":
            try:
                valid_duration = type(value) in {int, float} and math.isfinite(value) and value > 0
            except OverflowError:
                valid_duration = False
            if not valid_duration:
                raise ValueError("max_duration_seconds must be positive and finite")
        elif key in {"tool_packages", "skills_dirs"}:
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value) or len(set(value)) != len(value):
                raise ValueError(f"{key} must be a unique string array")
            if key == "tool_packages" and any(name not in BUILTIN_PACKAGES for name in value):
                raise ValueError("unknown tool package in configuration")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a nonempty string")
        if key == "request_bound_profile" and value != "deepseek-context-v1":
            raise ValueError("unknown request_bound_profile")
    for key in PATH_FIELDS & values.keys():
        values[key] = (path.parent / values[key]).resolve()
    if "skills_dirs" in values:
        values["skills_dir"] = [(path.parent / directory).resolve() for directory in values.pop("skills_dirs")]
    if "tool_packages" in values:
        values["tool_package"] = values.pop("tool_packages")
    return values
