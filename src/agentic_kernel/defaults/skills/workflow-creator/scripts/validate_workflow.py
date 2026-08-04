#!/usr/bin/env python3
"""Validate one agent-guided AMK workflow against its schema and tool catalog."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
REFERENCE_PATTERN = re.compile(r"\$\{([^}]+)\}")
PARAMETER_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
FALLBACK_ACTIONS = {"stop", "continue", "fail_partial"}
RETRY_EVENTS = {"retryable", "timeout", "transport"}
RUNTIME_VARIABLES = {"local_date", "now", "timezone", "scheduled_for"}


def _tool_index(explicit: Path | None) -> Path | None:
    if explicit:
        return explicit.resolve()
    roots = [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
    for root in roots:
        candidate = root / "tools" / "index.json"
        if candidate.is_file():
            return candidate
    return None


def _tools(path: Path, errors: list[str]) -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"catalogue d’outils illisible {path}: {exc}")
        return {}
    return {
        tool["name"]: tool
        for module in document.get("modules", [])
        for tool in module.get("tools", [])
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }


def _is_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(dependency) for dependency in graph[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def validate(document: Any, catalog: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["la racine YAML doit être un objet"]
    if document.get("schema") != "amk.workflow/v1":
        errors.append("schema doit valoir amk.workflow/v1")
    workflow_id = document.get("id")
    if not isinstance(workflow_id, str) or not ID_PATTERN.fullmatch(workflow_id):
        errors.append("id doit être en minuscules avec chiffres et tirets")
    if not isinstance(document.get("title"), str) or not document["title"].strip():
        errors.append("title doit être une chaîne non vide")
    status = document.get("status")
    if status not in {"draft", "ready", "blocked"}:
        errors.append("status doit être draft, ready ou blocked")

    execution = document.get("execution")
    if not isinstance(execution, dict):
        errors.append("execution doit être un objet")
    else:
        if execution.get("mode") != "agent_guided":
            errors.append("execution.mode doit valoir agent_guided")
        if execution.get("deviation") not in {
            "stop_and_report",
            "allow_declared_fallbacks",
        }:
            errors.append("execution.deviation est invalide")

    parameters = document.get("parameters", {})
    if not isinstance(parameters, dict):
        errors.append("parameters doit être un objet")
        parameters = {}
    for name, definition in parameters.items():
        if not isinstance(definition, dict) or definition.get("type") not in PARAMETER_TYPES:
            errors.append(f"parameters.{name} doit déclarer un type supporté")
            continue
        if "default" in definition and not _is_type(definition["default"], definition["type"]):
            errors.append(f"parameters.{name}.default ne respecte pas son type")

    variables = document.get("variables", {})
    if not isinstance(variables, dict):
        errors.append("variables doit être un objet")
        variables = {}
    for name, definition in variables.items():
        source = definition.get("from") if isinstance(definition, dict) else None
        if not isinstance(source, str):
            errors.append(f"variables.{name}.from est requis")
        elif source.startswith("runtime.") and source.split(".", 1)[1] not in RUNTIME_VARIABLES:
            errors.append(f"variables.{name}.from référence un champ runtime inconnu")

    permissions = document.get("permissions")
    declarations: list[dict[str, Any]] = []
    if not isinstance(permissions, dict):
        errors.append("permissions doit être un objet")
    else:
        if permissions.get("authority") != "kernel_guardian":
            errors.append("permissions.authority doit valoir kernel_guardian")
        if permissions.get("unlisted") != "stop_and_report":
            errors.append("permissions.unlisted doit valoir stop_and_report")
        raw_declarations = permissions.get("declarations", [])
        if not isinstance(raw_declarations, list):
            errors.append("permissions.declarations doit être une liste")
        else:
            declarations = [item for item in raw_declarations if isinstance(item, dict)]
            if len(declarations) != len(raw_declarations):
                errors.append("chaque permission doit être un objet")

    missing = document.get("missing_dependencies", [])
    if not isinstance(missing, list):
        errors.append("missing_dependencies doit être une liste")
        missing = []
    elif any(
        not isinstance(item, dict)
        or not isinstance(item.get("capability"), str)
        or not isinstance(item.get("reason"), str)
        for item in missing
    ):
        errors.append("chaque dépendance manquante exige capability et reason")
    if status == "ready" and missing:
        errors.append("un workflow ready ne peut pas avoir de dépendance manquante")
    if status == "blocked" and not missing:
        errors.append("un workflow blocked doit documenter une dépendance manquante")

    steps = document.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps doit être une liste non vide")
        return errors
    ids = [step.get("id") for step in steps if isinstance(step, dict)]
    if len(ids) != len(steps) or any(not isinstance(item, str) for item in ids):
        errors.append("chaque étape doit avoir un id")
        return errors
    if len(set(ids)) != len(ids):
        errors.append("les ids d’étape doivent être uniques")
    known_ids = set(ids)
    graph: dict[str, list[str]] = defaultdict(list)
    tool_steps: list[tuple[str, str, dict[str, Any]]] = []

    for step in steps:
        step_id = step["id"]
        if not ID_PATTERN.fullmatch(step_id):
            errors.append(f"steps.{step_id}.id est invalide")
        kind = step.get("kind")
        if kind not in {"tool", "synthesize"}:
            errors.append(f"steps.{step_id}.kind doit être tool ou synthesize")
        needs = step.get("needs", [])
        if not isinstance(needs, list) or any(not isinstance(item, str) for item in needs):
            errors.append(f"steps.{step_id}.needs doit être une liste d’ids")
            needs = []
        unknown_needs = set(needs) - known_ids
        if unknown_needs:
            errors.append(f"steps.{step_id}.needs contient {sorted(unknown_needs)}")
        if step_id in needs:
            errors.append(f"steps.{step_id} dépend de lui-même")
        graph[step_id] = needs

        if kind == "tool":
            tool_name = step.get("tool")
            args = step.get("args")
            if not isinstance(tool_name, str):
                errors.append(f"steps.{step_id}.tool est requis")
                continue
            if not isinstance(args, dict):
                errors.append(f"steps.{step_id}.args doit être un objet")
                args = {}
            tool_steps.append((step_id, tool_name, args))
            descriptor = catalog.get(tool_name)
            if descriptor is None:
                errors.append(f"steps.{step_id}.tool inconnu dans le catalogue: {tool_name}")
            else:
                schema = descriptor.get("input_schema", {})
                properties = schema.get("properties", {})
                required = set(schema.get("required", []))
                if schema.get("additionalProperties") is False:
                    unknown_args = set(args) - set(properties)
                    if unknown_args:
                        errors.append(f"steps.{step_id}.args inconnus: {sorted(unknown_args)}")
                absent = required - set(args)
                if absent:
                    errors.append(f"steps.{step_id}.args requis absents: {sorted(absent)}")
            evidence = step.get("evidence")
            if not isinstance(evidence, dict) or evidence.get("required") is not True:
                errors.append(f"steps.{step_id}.evidence.required doit valoir true")
            retry = step.get("retry", {"attempts": 1, "on": []})
            attempts = retry.get("attempts") if isinstance(retry, dict) else None
            retry_on = retry.get("on", []) if isinstance(retry, dict) else []
            attempts_valid = (
                isinstance(attempts, int)
                and not isinstance(attempts, bool)
                and 1 <= attempts <= 3
            )
            if not attempts_valid:
                errors.append(f"steps.{step_id}.retry.attempts doit être compris entre 1 et 3")
            if not isinstance(retry_on, list) or set(retry_on) - RETRY_EVENTS:
                errors.append(f"steps.{step_id}.retry.on contient une valeur invalide")
            fallback = step.get("fallback", {"action": "stop"})
            action = fallback.get("action") if isinstance(fallback, dict) else None
            if action not in FALLBACK_ACTIONS:
                errors.append(f"steps.{step_id}.fallback.action est invalide")
        elif not isinstance(step.get("instructions"), str):
            errors.append(f"steps.{step_id}.instructions est requis pour synthesize")

    if _has_cycle(graph):
        errors.append("le graphe des étapes contient un cycle")

    declared_tools = {item.get("tool") for item in declarations}
    for step_id, tool_name, args in tool_steps:
        matches = [item for item in declarations if item.get("tool") == tool_name]
        if not matches:
            errors.append(f"steps.{step_id}.tool n’est pas déclaré dans permissions: {tool_name}")
            continue
        fixed = matches[0].get("fixed_args", {})
        fixed_mismatch = not isinstance(fixed, dict) or any(
            args.get(key) != value for key, value in fixed.items()
        )
        if fixed_mismatch:
            errors.append(f"steps.{step_id} ne respecte pas permissions.fixed_args")
    for tool_name in declared_tools - {item[1] for item in tool_steps}:
        errors.append(f"permission inutilisée: {tool_name}")

    for text in _walk_strings(document):
        for reference in REFERENCE_PATTERN.findall(text):
            root, _, tail = reference.partition(".")
            if root == "parameters" and tail not in parameters:
                errors.append(f"référence de paramètre inconnue: {reference}")
            elif root == "variables" and tail not in variables:
                errors.append(f"référence de variable inconnue: {reference}")
            elif root == "steps" and tail.split(".", 1)[0] not in known_ids:
                errors.append(f"référence d’étape inconnue: {reference}")
            elif root not in {"parameters", "variables", "steps"}:
                errors.append(f"référence non supportée: {reference}")

    output = document.get("output")
    if not isinstance(output, dict):
        errors.append("output doit être un objet")
    else:
        if output.get("format") not in {"markdown", "text", "json"}:
            errors.append("output.format doit être markdown, text ou json")
        sections = output.get("sections")
        if not isinstance(sections, list) or not sections:
            errors.append("output.sections doit être une liste non vide")
        if output.get("evidence_policy") not in {
            "cite_or_mark_uncertain",
            "captured_only",
        }:
            errors.append("output.evidence_policy est invalide")
        if output.get("on_incomplete") not in {"fail", "partial_with_warnings"}:
            errors.append("output.on_incomplete est invalide")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path)
    parser.add_argument("--tools-index", type=Path)
    args = parser.parse_args()
    catalog_errors: list[str] = []
    index = _tool_index(args.tools_index)
    if index is None:
        print("ERREUR: tools/index.json introuvable; utiliser --tools-index", file=sys.stderr)
        return 2
    catalog = _tools(index, catalog_errors)
    try:
        document = yaml.safe_load(args.workflow.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"ERREUR: workflow illisible: {exc}", file=sys.stderr)
        return 2
    errors = [*catalog_errors, *validate(document, catalog)]
    if errors:
        for error in errors:
            print(f"ERREUR: {error}", file=sys.stderr)
        return 1
    status = document["status"]
    readiness = "prêt à être exécuté" if status == "ready" else f"non exécutable: {status}"
    print(
        f"Workflow structurellement valide: {document['id']} "
        f"({len(document['steps'])} étapes, mode agent_guided; {readiness})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
