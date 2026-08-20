from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from .auth import SPECS, OAuthManager
from .bootstrap import ensure_content_root, missing_configuration
from .config import ProjectConfig
from .doctor import diagnose
from .errors import KernelError
from .kernel import Kernel
from .managed_tools import (
    ManagedToolInstaller,
    discovered_codegraph_executable,
    discovered_executable,
    install_codegraph,
)
from .models import RunRequest, RunStatus, SecurityMode
from .modules import ModuleRegistry
from .paths import application_root, runtime_layout
from .platform.sandbox import docker_sandbox_enabled, ensure_docker_ready
from .providers import ProviderFactory
from .scheduler import CronService
from .searxng import SearxngService
from .web_launcher import run_web

app = typer.Typer(help="Agentic Markdown Kernel")
auth_app = typer.Typer(help="Manage OAuth credentials")
providers_app = typer.Typer(help="Inspect model providers")
agents_app = typer.Typer(help="Inspect agent definitions")
modules_app = typer.Typer(help="Manage modular capabilities")
skills_app = typer.Typer(help="Inspect OpenAI/Claude Agent Skills")
approvals_app = typer.Typer(help="Inspect and resolve guardian approvals")
crons_app = typer.Typer(help="Inspect scheduled routines")
sandbox_app = typer.Typer(help="Inspect and configure the execution sandbox")
app.add_typer(auth_app, name="auth")
app.add_typer(providers_app, name="providers")
app.add_typer(agents_app, name="agents")
app.add_typer(modules_app, name="modules")
app.add_typer(skills_app, name="skills")
app.add_typer(approvals_app, name="approvals")
app.add_typer(crons_app, name="crons")
app.add_typer(sandbox_app, name="sandbox")


def _root() -> Path:
    return application_root(Path.cwd())


@app.command("init")
def init_workspace() -> None:
    """Install the reference content into content-agents/ without overwriting."""
    content_root = ProjectConfig(_root()).content_root
    report = ensure_content_root(content_root)
    typer.echo(report.render())
    # `amk init` est explicite : on y détaille aussi la configuration optionnelle.
    pending = missing_configuration(content_root, include_optional=True)
    if pending:
        typer.echo("")
        typer.echo("Configuration à compléter :")
        for name in pending:
            example = name.replace(".json", ".example.json")
            typer.echo(f"  cp content-agents/{example} content-agents/{name}")
        typer.echo("")
        typer.echo("Puis renseigner la clé du provider, ou définir DEEPSEEK_API_KEY.")


@app.command("setup")
def setup(  # noqa: C901 - dette: installation multi-étapes
    full: Annotated[
        bool,
        typer.Option("--full", help="Also install llama.cpp and download the vision model"),
    ] = False,
    no_downloads: Annotated[
        bool,
        typer.Option("--no-downloads", help="Only create/update AMK user content"),
    ] = False,
) -> None:
    """Prepare a usable local AMK installation."""
    layout = runtime_layout()
    report = ensure_content_root(layout.content_root)
    typer.echo(report.render())
    if no_downloads:
        return
    installer = ManagedToolInstaller()
    ketch = discovered_executable("ketch", "AMK_KETCH_BIN")
    if ketch:
        typer.echo(f"Ketch existant réutilisé : {ketch}")
    else:
        typer.echo("Installation de Ketch…")
        typer.echo(f"  {installer.install('ketch')}")

    npm = shutil.which("npm")
    if npm is None:
        raise KernelError("npm est requis pour préparer la surface web et CodeGraph")

    codegraph = discovered_codegraph_executable()
    if codegraph:
        typer.echo(f"CodeGraph existant réutilisé : {codegraph}")
    else:
        typer.echo("Installation de CodeGraph…")
        typer.echo(f"  {install_codegraph(npm)}")

    web_root = layout.application_root / "surfaces" / "web"
    if not (web_root / "node_modules").is_dir():
        typer.echo("Installation des dépendances de la surface web…")
        subprocess.run([npm, "ci", "--prefix", str(web_root)], check=True)  # noqa: S603 - commande fixe

    typer.echo("Préparation de SearXNG…")
    searxng = SearxngService()
    try:
        installed_searxng = searxng.prepare()
    except (OSError, subprocess.SubprocessError) as exc:
        raise KernelError(f"impossible de préparer SearXNG : {exc}") from exc
    if installed_searxng:
        typer.echo(f"  SearXNG installé et configuré sur {searxng.base_url}")
    else:
        typer.echo(f"  SearXNG existant réutilisé sur {searxng.base_url}")

    if not full:
        typer.echo("Vision locale optionnelle : amk setup --full")
        return
    from .vision import LocalVisionService

    service = LocalVisionService(layout.content_root)
    llama, gemma = service.installed_assets()
    if llama:
        typer.echo(f"llama.cpp existant réutilisé : {llama}")
    else:
        typer.echo("Installation de llama.cpp…")
        typer.echo(f"  {installer.install('llama')}")
    if gemma:
        typer.echo(f"Gemma 4 existant réutilisé : {gemma}")
    else:
        typer.echo("Téléchargement du modèle vision…")
    typer.echo("Préparation de la vision locale…")
    try:
        asyncio.run(service.prepare())
    finally:
        service.close()
    typer.echo("Vision locale prête.")


@app.command("doctor")
def doctor() -> None:
    """Inspect installation, security and optional local capabilities."""
    checks = diagnose(runtime_layout())
    for check in checks:
        typer.echo(f"{check.status:10} {check.name:24} {check.detail}")
    if any(check.required and check.status in {"missing", "error"} for check in checks):
        raise typer.Exit(1)


@app.command("run")
def run_agent(
    prompt: str = typer.Argument(..., help="Task to execute"),
    agent: str = typer.Option("main", "--agent", "-a"),
    skill: Annotated[list[str] | None, typer.Option("--skill", "-s")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
    security_mode: Annotated[SecurityMode, typer.Option("--security-mode")] = SecurityMode.LIMITED,
) -> None:
    """Run an agent and print its final output."""
    try:
        kernel = Kernel(_root())
        result = asyncio.run(
            kernel.run(
                RunRequest(
                    prompt=prompt,
                    agent_id=agent,
                    skills=skill or [],
                    workspace=(workspace or Path.cwd()),
                    security_mode=security_mode,
                )
            )
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
        typer.echo(f"{item.approval_id}\t{item.agent_id}\t{item.tool_name}\t{item.path or '-'}")


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
        typer.echo(f"{provider.id}\t{provider.connection_type.value}\t{provider.model}{default}")


@providers_app.command("check")
def providers_check(provider: str | None = None) -> None:
    project = ProjectConfig(_root())
    registry = project.providers()
    factory = ProviderFactory(
        registry,
        runtime_dir=project.content_root / "runtime" / "providers",
    )
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


def _provider_factory() -> ProviderFactory:
    project = ProjectConfig(_root())
    return ProviderFactory(
        project.providers(),
        runtime_dir=project.content_root / "runtime" / "providers",
    )


@providers_app.command("status")
def providers_status(provider: str | None = None) -> None:
    """Show managed llama.cpp processes without starting them."""
    factory = _provider_factory()
    ids = [provider] if provider else [item.id for item in factory.registry.providers]
    found = False
    for provider_id in ids:
        manager = factory.managed_llama(provider_id)
        if manager is None:
            if provider:
                typer.echo(f"{provider_id}: not a managed llama.cpp provider")
            continue
        found = True
        state = manager.status()
        if state is None:
            typer.echo(f"{provider_id}: stopped (log: {manager.log_path()})")
        else:
            health = "healthy" if manager.health_ok() else "unhealthy"
            typer.echo(
                f"{provider_id}: {health} model={state.model} pid={state.pid} port={state.port}"
            )
    if provider and not found:
        raise typer.Exit(1)


@providers_app.command("start")
def providers_start(provider: str, model: str | None = None) -> None:
    """Start or switch a managed llama.cpp provider."""
    factory = _provider_factory()
    config = factory.get_config(provider)
    manager = factory.managed_llama(provider)
    if manager is None:
        typer.echo(f"error: {provider} is not a managed llama.cpp provider", err=True)
        raise typer.Exit(2)
    selected = model or config.model
    if not selected:
        typer.echo("error: select a model", err=True)
        raise typer.Exit(2)
    try:
        state = manager.ensure_running(selected)
    except Exception as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"{provider}: ready model={state.model} pid={state.pid} port={state.port}")


@providers_app.command("stop")
def providers_stop(provider: str) -> None:
    """Stop a managed llama.cpp provider."""
    manager = _provider_factory().managed_llama(provider)
    if manager is None:
        typer.echo(f"error: {provider} is not a managed llama.cpp provider", err=True)
        raise typer.Exit(2)
    typer.echo(f"{provider}: {'stopped' if manager.stop() else 'already stopped'}")


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
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    """Serve the kernel HTTP API for local surfaces."""
    import uvicorn

    _bootstrap_on_start()
    uvicorn.run(create_api(_root(), workspace), host=host, port=port)


@app.command("start")
@app.command("web")
def web(
    host: str = typer.Option("127.0.0.1", help="Bind address for both services"),
    api_port: int = typer.Option(8765, help="Kernel API port"),
    web_port: int = typer.Option(3000, help="Web surface port"),
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
    check: bool = typer.Option(
        True,
        "--check/--no-check",
        help="Vérifier lint, types et contraste en arrière-plan pendant l'exécution",
    ),
) -> None:
    """Démarre l'API AMK et la surface web ensemble.

    `agents start` et `amk web` désignent la même commande : la seconde reste
    en place pour ne pas casser les habitudes et les scripts existants.
    """
    _bootstrap_on_start()
    try:
        status = run_web(
            _root(),
            _default_web_workspace(workspace),
            host,
            api_port,
            web_port,
            check=check,
        )
    except KernelError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if status:
        raise typer.Exit(status)


def _cron_service() -> CronService:
    return CronService(ProjectConfig(_root()).content_root / "state.db")


@crons_app.command("list")
def crons_list() -> None:
    """List routines, flagging those whose workspace is missing here."""
    for job in _cron_service().list():
        state = "active" if job.enabled else "inactive"
        typer.echo(f"{job.id}  {state:8}  {job.schedule:16}  {job.name}")
        if job.workspace and not job.workspace.is_dir():
            typer.echo(f"    workspace absent sur cette machine : {job.workspace}")


def _bootstrap_on_start() -> None:
    """Installe le socle avant de démarrer, et ne parle que s'il a agi.

    Appelé depuis les commandes qui lancent réellement l'application, pas
    depuis `create_app` : instancier le kernel dans un test ne doit pas écrire
    dans le système de fichiers.
    """
    if docker_sandbox_enabled():
        ready, started, detail = ensure_docker_ready()
        if not ready:
            raise KernelError(
                "Docker est requis pour le sandbox mais n’a pas pu démarrer : " + detail
            )
        if started:
            typer.echo(f"Docker Desktop démarré : {detail}", err=True)
    content_root = ProjectConfig(_root()).content_root
    report = ensure_content_root(content_root)
    if report.initialized:
        typer.echo(report.render(), err=True)
    searxng = SearxngService()
    try:
        installed = searxng.prepare()
    except (OSError, subprocess.SubprocessError) as exc:
        raise KernelError(f"impossible de préparer SearXNG : {exc}") from exc
    if installed:
        typer.echo(f"SearXNG installé et configuré sur {searxng.base_url}", err=True)
    for name in missing_configuration(content_root):
        example = name.replace(".json", ".example.json")
        typer.echo(
            f"note: content-agents/{name} est absent. "
            f"Copier content-agents/{example} et le renseigner.",
            err=True,
        )
    # Une routine dont le dossier a disparu reste visible et modifiable : c'est
    # à l'exécution qu'elle échouera, avec un message qui nomme le chemin.
    try:
        stale = [
            job
            for job in CronService(content_root / "state.db").list()
            if job.workspace and not job.workspace.is_dir()
        ]
    except Exception:  # un état illisible ne doit pas bloquer le démarrage
        return
    for job in stale:
        typer.echo(
            f"note: routine « {job.name} » — workspace absent : {job.workspace}",
            err=True,
        )


def _default_web_workspace(explicit: Path | None) -> Path:
    if explicit is not None:
        selected = explicit.expanduser().resolve()
        if not selected.is_dir():
            raise KernelError(f"workspace absent : {selected}")
        return selected
    current = Path.cwd().resolve()
    if any((parent / ".git").exists() for parent in (current, *current.parents)):
        return current
    neutral = runtime_layout().content_root / "workspaces" / "main"
    neutral.mkdir(parents=True, exist_ok=True)
    return neutral.resolve()


def create_api(root: Path, workspace: Path | None = None):
    from .api import create_app

    return create_app(root, workspace, local_services=True)
