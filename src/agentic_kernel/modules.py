from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from .errors import ModuleError
from .models import ModuleIndex, ModuleManifest, ToolResult


class KernelModule(Protocol):
    def toolsets(self) -> list[Any]: ...

    def instructions(self) -> list[str]: ...

    def capabilities(self) -> list[Any]: ...


class ModuleRegistry:
    def __init__(self, tools_root: Path) -> None:
        self.tools_root = tools_root
        self.modules_root = tools_root / "modules"
        self.index_path = tools_root / "index.json"

    def discover(self) -> ModuleIndex:
        manifests: list[ModuleManifest] = []
        seen: set[str] = set()
        seen_tools: set[str] = set()
        for path in sorted(self.modules_root.glob("*/module.json")):
            try:
                manifest = ModuleManifest.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError) as exc:
                raise ModuleError(f"invalid module manifest {path}: {exc}") from exc
            if manifest.id in seen:
                raise ModuleError(f"duplicate module id: {manifest.id}")
            duplicate_tools = seen_tools & {tool.name for tool in manifest.tools}
            if duplicate_tools:
                raise ModuleError(f"duplicate tool ids: {sorted(duplicate_tools)}")
            if path.parent.name != manifest.id:
                raise ModuleError(
                    f"module directory {path.parent.name!r} must match id {manifest.id!r}"
                )
            entrypoint = path.parent / manifest.entrypoint.partition(":")[0]
            if not entrypoint.is_file():
                raise ModuleError(f"missing entrypoint for {manifest.id}: {entrypoint}")
            seen.add(manifest.id)
            seen_tools.update(tool.name for tool in manifest.tools)
            manifests.append(manifest)
        manifests.sort(key=lambda item: item.id)
        return ModuleIndex(modules=manifests)

    def build_index(self) -> ModuleIndex:
        index = self._resolved_index()
        self._validate_contracts(index)
        self.tools_root.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(index.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.index_path)
        return index

    def check_index(self) -> ModuleIndex:
        expected = self._resolved_index()
        self._validate_contracts(expected)
        try:
            actual = ModuleIndex.model_validate_json(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise ModuleError(f"invalid or missing module index: {exc}") from exc
        if actual != expected:
            raise ModuleError("tools/index.json is stale; run `amk modules build-index`")
        for manifest in actual.modules:
            if manifest.enabled:
                self._load_one(manifest)
        return actual

    def _resolved_index(self) -> ModuleIndex:
        """Build the public catalog from manifests plus executable schemas."""
        discovered = self.discover()
        manifests: list[ModuleManifest] = []
        for manifest in discovered.modules:
            instance = self._load_one(manifest)
            runtime_tools: dict[str, Any] = {}
            for toolset in instance.toolsets():
                tools = getattr(toolset, "tools", None)
                if isinstance(tools, dict):
                    runtime_tools.update(tools)
            descriptors = []
            for descriptor in manifest.tools:
                runtime = runtime_tools.get(descriptor.name)
                schema = getattr(runtime, "function_schema", None)
                descriptors.append(
                    descriptor.model_copy(
                        update={
                            "input_schema": descriptor.input_schema
                            or dict(getattr(schema, "json_schema", {}) or {}),
                            "output_schema": descriptor.output_schema
                            or ToolResult.model_json_schema(),
                            "timeout_seconds": descriptor.timeout_seconds
                            or getattr(runtime, "timeout", None),
                        }
                    )
                )
            manifests.append(manifest.model_copy(update={"tools": descriptors}))
        return ModuleIndex(modules=manifests)

    @staticmethod
    def _validate_contracts(index: ModuleIndex) -> None:
        """Fail closed when an indexed tool cannot honor the kernel contract."""
        expected_result_fields = {"ok", "data", "error", "metadata"}
        for manifest in index.modules:
            for tool in manifest.tools:
                qualified = f"{manifest.id}.{tool.name}"
                if tool.input_schema.get("type") != "object":
                    raise ModuleError(f"{qualified} input_schema must describe an object")
                if tool.output_schema.get("type") != "object":
                    raise ModuleError(f"{qualified} output_schema must describe an object")
                properties = tool.output_schema.get("properties", {})
                if not expected_result_fields <= set(properties):
                    raise ModuleError(
                        f"{qualified} output_schema must implement ToolResult"
                    )
                if tool.timeout_seconds is None:
                    raise ModuleError(f"{qualified} requires a bounded timeout")

    def load(self, ids: list[str]) -> list[KernelModule]:
        index = self.check_index()
        available = {manifest.id: manifest for manifest in index.modules if manifest.enabled}
        unknown = set(ids) - available.keys()
        if unknown:
            raise ModuleError(f"unknown or disabled modules: {sorted(unknown)}")
        return [self._load_one(available[module_id]) for module_id in ids]

    def load_enabled(self) -> list[tuple[ModuleManifest, KernelModule]]:
        index = self.check_index()
        return [
            (manifest, self._load_one(manifest)) for manifest in index.modules if manifest.enabled
        ]

    def _load_one(self, manifest: ModuleManifest) -> KernelModule:
        filename, _, symbol = manifest.entrypoint.partition(":")
        symbol = symbol or "module"
        path = self.modules_root / manifest.id / filename
        spec = importlib.util.spec_from_file_location(f"amk_module_{manifest.id}", path)
        if spec is None or spec.loader is None:
            raise ModuleError(f"cannot import module {manifest.id}")
        python_module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(python_module)
            instance = getattr(python_module, symbol)
        except Exception as exc:
            raise ModuleError(f"cannot load module {manifest.id}: {exc}") from exc
        for method in ("toolsets", "instructions", "capabilities"):
            if not callable(getattr(instance, method, None)):
                raise ModuleError(f"module {manifest.id} does not implement {method}()")
        declared = {tool.name for tool in manifest.tools}
        actual: set[str] = set()
        for toolset in instance.toolsets():
            tools = getattr(toolset, "tools", None)
            if isinstance(tools, dict):
                actual.update(tools)
        if declared and actual != declared:
            raise ModuleError(
                f"module {manifest.id} tools differ from manifest: "
                f"declared={sorted(declared)}, actual={sorted(actual)}"
            )
        return instance
