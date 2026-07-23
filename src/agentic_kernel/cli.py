from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from .auth import SPECS, OAuthManager
from .config import ProjectConfig
from .errors import KernelError
from .kernel import Kernel
from .models import RunRequest, RunStatus, SecurityMode
from .modules import ModuleRegistry
from .providers import ProviderFactory
from .web_launcher import run_web

app = typer.Typer(help="Agentic Markdown Kernel")
auth_app = typer.Typer(help="Manage OAuth credentials")
providers_app = typer.Typer(help="Inspect model providers")
agents_app = typer.Typer(help="Inspect agent definitions")
modules_app = typer.Typer(help="Manage modular capabilities")
skills_app = typer.Typer(help="Inspect OpenAI/Claude Agent Skills")
approvals_app = typer.Typer(help="Inspect and resolve guardian approvals")
app.add_typer(auth_app, name="auth")
app.add_typer(providers_app, name="providers")
app.add_typer(agents_app, name="agents")
app.add_typer(modules_app, name="modules")
app.add_typer(skills_app, name="skills")
app.add_typer(approvals_app, name="approvals")


def _root() -> Path:
    return Path.cwd()


@app.command("run")
def run_agent(
    prompt: str = typer.Argument(..., help="Task to execute"),
    agent: str = typer.Option("main", "--agent", "-a"),
    skill: Annotated[list[str] | None, typer.Option("--skill", "-s")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
    security_mode: Annotated[
        SecurityMode, typer.Option("--security-mode")
    ] = SecurityMode.LIMITED,
) -> None:
    """Run an agent and print its final output."""
    try:
        kernel = Kernel(_root())
        result = asyncio.run(
            kernel.run(RunRequest(
                prompt=prompt, agent_id=agent, skills=skill or [],
                workspace=(workspace or Path.cwd()), security_mode=security_mode,
            ))
        )
        while result.status is RunStatus.APPROVAL_PENDING:
            pending = [
                item for item in kernel.list_approvals() if item.session_id == result.session_id
            ]
            if not pending:
                break
            item = pending[0]
            typer.echo(
                f"Approval required: {item.tool_name} {item.path or ''}\n"
                f"Reason: {item.reason}\nJustification: {item.justification}"
            )
            approved = typer.confirm("Authorize this action?", default=False)
            result = asyncio.run(kernel.resolve_approval(item.approval_id, approved))
    except KernelError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if result.output:
        typer.echo(result.output)
    if result.status != RunStatus.SUCCESS:
        for error in result.errors:
            typer.echo(f"{error.type}: {error.message}", err=True)
        raise typer.Exit(1)


@approvals_app.command("list")
def approvals_list() -> None:
    for item in Kernel(_root()).list_approvals():
        typer.echo(
            f"{item.approval_id}\t{item.agent_id}\t{item.tool_name}\t{item.path or '-'}"
        )


@approvals_app.command("resolve")
def approvals_resolve(approval_id: str, approve: bool = typer.Option(False, "--approve")) -> None:
    result = asyncio.run(Kernel(_root()).resolve_approval(approval_id, approve))
    typer.echo(result.model_dump_json(indent=2))


@auth_app.command("login")
def auth_login(provider: str) -> None:
    """Authenticate an OAuth provider in the system keyring."""
    try:
        OAuthManager().login(provider)
    except KernelError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"authenticated: {provider}")


@auth_app.command("logout")
def auth_logout(provider: str) -> None:
    OAuthManager().logout(provider)
    typer.echo(f"logged out: {provider}")


@auth_app.command("status")
def auth_status(provider: str | None = None) -> None:
    manager = OAuthManager()
    provider_ids = [provider] if provider else sorted(SPECS)
    for provider_id in provider_ids:
        state = "authenticated" if manager.status(provider_id) else "not authenticated"
        typer.echo(f"{provider_id}: {state}")


@providers_app.command("list")
def providers_list() -> None:
    registry = ProjectConfig(_root()).providers()
    for provider in registry.providers:
        default = " *" if provider.id == registry.default_provider else ""
        typer.echo(
            f"{provider.id}\t{provider.connection_type.value}\t{provider.model}{default}"
        )


@providers_app.command("check")
def providers_check(provider: str | None = None) -> None:
    registry = ProjectConfig(_root()).providers()
    factory = ProviderFactory(registry)
    ids = [provider] if provider else [item.id for item in registry.providers]

    async def check_all():
        return [(provider_id, *await factory.check(provider_id)) for provider_id in ids]

    results = asyncio.run(check_all())
    failed = False
    for provider_id, ok, detail in results:
        typer.echo(f"{provider_id}: {'ok' if ok else 'failed'} ({detail})")
        failed = failed or not ok
    if failed:
        raise typer.Exit(1)


@agents_app.command("list")
def agents_list() -> None:
    for agent in ProjectConfig(_root()).agents().values():
        typer.echo(f"{agent.id}\t{agent.provider}\t{agent.description}")


@agents_app.command("validate")
def agents_validate() -> None:
    config = ProjectConfig(_root())
    agents = config.agents()
    modules = ModuleRegistry(config.tools_root)
    modules.check_index()
    available = {item.id for item in modules.discover().modules if item.enabled}
    available_skills = set(config.skills())
    for agent in agents.values():
        unknown = set(agent.modules) - available
        if unknown:
            raise typer.BadParameter(f"agent {agent.id} uses unknown modules {sorted(unknown)}")
        unknown_skills = set(agent.skills) - available_skills
        if unknown_skills:
            raise typer.BadParameter(
                f"agent {agent.id} uses unknown skills {sorted(unknown_skills)}"
            )
    typer.echo(f"valid: {len(agents)} agent(s)")


@modules_app.command("build-index")
def modules_build_index() -> None:
    index = ModuleRegistry(ProjectConfig(_root()).tools_root).build_index()
    typer.echo(f"indexed: {len(index.modules)} module(s)")


@modules_app.command("check")
def modules_check() -> None:
    index = ModuleRegistry(ProjectConfig(_root()).tools_root).check_index()
    typer.echo(f"valid: {len(index.modules)} module(s)")


@skills_app.command("list")
def skills_list() -> None:
    for skill in ProjectConfig(_root()).skills().values():
        typer.echo(f"{skill.name}\t{skill.description}\t{skill.source}")


@skills_app.command("validate")
def skills_validate() -> None:
    skills = ProjectConfig(_root()).skills()
    typer.echo(f"valid: {len(skills)} skill(s)")


@app.command("session")
def session_dump(session_id: str) -> None:
    """Print a session JSONL path and validated events."""
    from uuid import UUID

    kernel = Kernel(_root())
    events = kernel.events.read(UUID(session_id))
    typer.echo(json.dumps([event.model_dump(mode="json") for event in events], indent=2))


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8765, help="Bind port"),
) -> None:
    """Serve the kernel HTTP API for local surfaces."""
    import uvicorn

    uvicorn.run(create_api(_root()), host=host, port=port)


@app.command("web")
def web(
    host: str = typer.Option("127.0.0.1", help="Bind address for both services"),
    api_port: int = typer.Option(8765, help="Kernel API port"),
    web_port: int = typer.Option(3000, help="Web surface port"),
) -> None:
    """Restart and run the AMK API and web surface together."""
    try:
        status = run_web(_root(), host, api_port, web_port)
    except KernelError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if status:
        raise typer.Exit(status)


def create_api(root: Path):
    from .api import create_app

    return create_app(root)
