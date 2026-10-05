"""Configured completion requirements checked against actual workflow evidence."""

from copy import deepcopy

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from baseagent.tools.result import ToolFailure


_string = {"type": "string", "minLength": 1, "maxLength": 4000}
_paths = {"type": "array", "maxItems": 20, "uniqueItems": True, "items": _string}
_schema = {"type": "object", "additionalProperties": False, "required": ["require_plan", "checks", "artifacts"],
           "properties": {"require_plan": {"type": "boolean"}, "artifacts": _paths,
                          "checks": {"type": "array", "maxItems": 20, "items": {
                              "type": "object", "additionalProperties": False, "required": ["name", "argv", "paths"],
                              "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 100},
                                             "argv": {"type": "array", "minItems": 1, "maxItems": 128, "items": _string}, "paths": _paths}}}}}


class CompletionPolicy:
    def __init__(self, workspace, requirements):
        try:
            Draft202012Validator(_schema).validate(requirements)
        except ValidationError as exc:
            raise ValueError("invalid completion requirements: " + exc.validator) from exc
        if len({check["name"] for check in requirements["checks"]}) != len(requirements["checks"]):
            raise ValueError("completion check names must be unique")
        self.workspace = workspace
        self.requirements = deepcopy(requirements)
        for path in self.requirements["artifacts"] + [path for check in self.requirements["checks"] for path in check["paths"]]:
            try:
                workspace._path(path)
            except ToolFailure as exc:
                raise ValueError("completion paths must remain within the permitted workspace") from exc

    def contract(self):
        return {"version": 1, "root": str(self.workspace.root), "requirements": deepcopy(self.requirements)}

    def instructions(self):
        return {"completion_requirements": deepcopy(self.requirements),
                "instruction": "Before a final answer, complete the required plan and execute every configured argv using verify_command with its paths. Requirements are enforced against current-turn actual command records and current file hashes."}

    def evaluate(self, state):
        issues, evidence = [], {}
        if self.requirements["require_plan"] and (not state.plan or any(step["status"] != "completed" for step in state.plan)):
            issues.append("task plan is missing or has unfinished steps")
        for spec in self.requirements["checks"]:
            record = next((record for record in reversed(state.verifications)
                           if record.get("turn_id") == state.turn_id and record["argv"] == spec["argv"]), None)
            if not record or record["status"] != "passed" or record["exit_code"] != 0:
                issues.append("required check has no current successful execution: " + spec["name"])
                continue
            valid = True
            for path in spec["paths"]:
                try:
                    relative = str(self.workspace._path(path).relative_to(self.workspace.root))
                    current = self.workspace.file_hash(path)
                    expected = record["hashes_after"].get(relative)
                    if current == "missing" or expected != current or record["hashes_before"].get(relative) != current:
                        valid = False
                except (OSError, ToolFailure):
                    valid = False
            if not valid:
                issues.append("required check is missing tracked files or its evidence is stale: " + spec["name"])
            else:
                evidence[spec["name"]] = record["id"]
        artifacts = {}
        for path in self.requirements["artifacts"]:
            try:
                value = self.workspace.file_hash(path)
                if value == "missing":
                    raise FileNotFoundError()
                artifacts[path] = value
            except (OSError, ToolFailure):
                issues.append("required artifact is missing or unreadable: " + path)
        return {"passed": not issues, "issues": issues, "checks": evidence, "artifacts": artifacts}
