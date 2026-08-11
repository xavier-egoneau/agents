from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .errors import ConfigurationError
from .platform.processes import (
    listening_pids,
    process_command,
    process_parent_pid,
    process_pids,
    subprocess_group_kwargs,
    terminate_tree,
)


def is_amk_web_launcher(pid: int) -> bool:
    """Reconnaît une instance d'AMK, surface ou serveur d'API.

    `amk web` lance `amk serve` en enfant. Quand le parent meurt sans l'emporter
    — fermeture brutale, terminal fermé —, le serveur survit et garde le port.
    Ne reconnaître que `web` laissait cet orphelin en travers du démarrage
    suivant, avec un message qui le disait « hors surface Web » : vrai de son
    argv, faux de sa nature.

    `agents start` reprend la machine : une seule instance à la fois, quel que
    soit le projet ou le port. Un `amk serve` lancé à la main tombe donc aussi,
    et c'est la politique voulue.
    """
    try:
        arguments = [item.strip('"') for item in shlex.split(process_command(pid), posix=False)]
    except ValueError:
        return False
    if not arguments:
        return False
    executable = arguments[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    values = [item.lower() for item in arguments[1:]]
    moitiés = {"web", "serve"}
    if executable in {"uv", "uv.exe"}:
        return (
            values[:2] in (["run", "amk"], ["run", "amk.exe"])
            and values[2:3] != []
            and values[2] in moitiés
        )
    if executable in {"amk", "amk.exe", "agents", "agents.exe"}:
        return values[:1] != [] and values[0] in moitiés
    if executable.startswith(("python", "pypy")):
        if values[:2] == ["-m", "agentic_kernel.cli"] and values[2:3] and values[2] in moitiés:
            return True
        if len(values) >= 2:
            script = values[0].replace("\\", "/").rsplit("/", 1)[-1]
            return script in {"amk", "amk.exe", "agents", "agents.exe"} and values[1] in moitiés
    return False


def _ancestors(pid: int) -> set[int]:
    result: set[int] = set()
    parent = process_parent_pid(pid)
    while parent and parent not in result:
        result.add(parent)
        parent = process_parent_pid(parent)
    return result


def stop_previous_instances() -> list[int]:
    """Stop every other ``amk web`` tree, regardless of project or port."""
    protected = {os.getpid(), *_ancestors(os.getpid())}
    candidates = {
        pid for pid in process_pids() if pid not in protected and is_amk_web_launcher(pid)
    }
    # ``uv run amk web`` may expose both uv and amk as matching processes.
    # Killing only the highest matching parent avoids touching the same tree twice.
    owned = sorted(pid for pid in candidates if not (_ancestors(pid) & candidates))
    for pid in owned:
        _terminate(pid)
    return owned


def ensure_api_port_available(port: int) -> None:
    """Ne reste ici que ce qui n'appartient pas à AMK.

    `stop_previous_instances` a déjà emporté les instances précédentes, serveur
    d'API compris. Ce qui tient encore le port est donc un programme tiers, et
    l'arrêter serait une décision qui ne nous revient pas.
    """
    conflicts = [pid for pid in listening_pids(port) if pid != os.getpid()]
    if not conflicts:
        return
    details = "; ".join(f"PID {pid} ({process_command(pid) or 'unknown'})" for pid in conflicts)
    raise ConfigurationError(
        f"port API {port} déjà utilisé par un processus hors surface Web : {details}"
    )


def available_web_port(
    root: Path,
    preferred: int,
    api_port: int,
    *,
    attempts: int = 100,
) -> int:
    """Keep an owned AMK port restartable, but never evict another application."""
    for port in range(preferred, preferred + attempts):
        pids = [pid for pid in listening_pids(port) if pid != os.getpid()]
        if not pids:
            return port
    raise ConfigurationError(
        f"no free web port found between {preferred} and {preferred + attempts - 1}"
    )


def _terminate(pid: int) -> None:
    terminate_tree(pid, 4)


def start_verification(npm: str, web_root: Path) -> subprocess.Popen[bytes] | None:
    """Lance lint, types et contraste en parallèle du serveur de développement.

    Bloquer le démarrage sur une vérification complète coûterait une minute à
    chaque lancement, y compris quand rien n'a changé. Le serveur démarre donc
    tout de suite et le verdict arrive quand il est prêt : on ne perd que la
    latence de l'information, jamais celle de l'application.
    """
    try:
        return subprocess.Popen(  # noqa: S603 - npm absolu, commande fixe
            [npm, "run", "verify"],
            cwd=web_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **subprocess_group_kwargs(),
        )
    except OSError:
        # La vérification est un confort : son échec ne doit pas empêcher de
        # travailler.
        return None


def report_verification(process: subprocess.Popen[bytes]) -> None:
    output = (process.communicate()[0] or b"").decode("utf-8", "replace")
    if process.returncode == 0:
        print("Vérification du front : lint, types et contraste — OK", flush=True)
        return
    print("\nVérification du front — problèmes détectés :", flush=True)
    # Seulement la fin : npm empile ses propres bannières avant l'erreur utile.
    lines = [line for line in output.splitlines() if line.strip()]
    print("\n".join(lines[-40:]), flush=True)
    print("Détail complet : npm run verify (depuis surfaces/web)\n", flush=True)


def run_web(  # noqa: C901 - dette: orchestration multi-services
    root: Path,
    workspace: Path,
    host: str,
    api_port: int,
    web_port: int,
    check: bool = True,
) -> int:
    root = root.resolve()
    workspace = workspace.resolve()
    # The index is derived data. Rebuild it before starting the long-lived
    # scheduler so a newly installed or edited module cannot poison cron runs
    # with a stale catalog from a previous process.
    from .modules import ModuleRegistry

    ModuleRegistry(root / "tools").build_index()
    web_root = root / "surfaces" / "web"
    if not (web_root / "package.json").is_file():
        raise ConfigurationError(
            f"surface web introuvable : {web_root}. Le dépôt est incomplet — "
            "vérifier que `surfaces/web` existe, ou relancer depuis la racine du projet."
        )
    npm = shutil.which("npm")
    if npm is None:
        raise ConfigurationError(
            "`npm` introuvable dans le PATH. Installer Node.js, puis rouvrir le "
            "terminal — une session ouverte avant l'installation garde l'ancien PATH."
        )
    amk = shutil.which("amk") or str(Path(sys.executable).parent / "amk")
    stopped = stop_previous_instances()
    if stopped:
        print(
            f"Stopped previous AMK Web instance(s): {', '.join(map(str, stopped))}",
            flush=True,
        )
    ensure_api_port_available(api_port)
    selected_web_port = available_web_port(root, web_port, api_port)
    if selected_web_port != web_port:
        print(
            f"Port {web_port} is used by another application; "
            f"AMK Web will use port {selected_web_port}.",
            flush=True,
        )
        web_port = selected_web_port
    backend = subprocess.Popen(  # noqa: S603 - binaire amk absolu, commande fixe
        [
            amk,
            "serve",
            "--host",
            host,
            "--port",
            str(api_port),
            "--workspace",
            str(workspace),
        ],
        cwd=root,
        **subprocess_group_kwargs(),
    )
    environment = {**os.environ, "AMK_KERNEL_URL": f"http://{host}:{api_port}"}
    frontend = subprocess.Popen(  # noqa: S603 - npm absolu, commande fixe
        [npm, "run", "dev", "--", "--host", host, "--port", str(web_port)],
        cwd=web_root,
        env=environment,
        **subprocess_group_kwargs(),
    )
    print(f"AMK API: http://{host}:{api_port}", flush=True)
    print(f"AMK Web: http://{host}:{web_port}", flush=True)
    verification = start_verification(npm, web_root) if check else None
    try:
        while True:
            backend_status = backend.poll()
            frontend_status = frontend.poll()
            if backend_status is not None:
                return backend_status
            if frontend_status is not None:
                return frontend_status
            if verification is not None and verification.poll() is not None:
                report_verification(verification)
                verification = None
            time.sleep(0.25)
    except KeyboardInterrupt:
        return 0
    finally:
        if verification is not None:
            _stop_group(verification)
        _stop_group(frontend)
        _stop_group(backend)


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    terminate_tree(process.pid, 4)
