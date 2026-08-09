from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agentic_kernel.models import SecurityMode
from agentic_kernel.platform.secure_files import secure_file

_SAFE_ENV = {
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TERM",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
}


@dataclass(frozen=True)
class SandboxCapabilities:
    backend: str
    process_tree_isolation: bool = False
    filesystem_isolation: bool = False
    network_isolation: bool = False

    @property
    def execution_isolated(self) -> bool:
        return self.process_tree_isolation and self.filesystem_isolation


@dataclass(frozen=True)
class PreparedExecution:
    command: list[str]
    env: dict[str, str]
    profile_path: Path | None
    sandboxed: bool
    backend: str

    def cleanup(self) -> None:
        if self.profile_path is not None:
            self.profile_path.unlink(missing_ok=True)


class SandboxBackend(Protocol):
    capabilities: SandboxCapabilities

    def prepare(
        self,
        command: list[str],
        deps: Any,
        runtime: RuntimeDirectories,
        *,
        allow_network: bool,
        publish_ports: list[int] | None = None,
    ) -> PreparedExecution: ...


@dataclass(frozen=True)
class RuntimeDirectories:
    workspace: Path
    events_root: Path
    runtime_root: Path
    home: Path
    temporary: Path
    cache: Path
    artifact_root: Path


class UnavailableSandbox:
    def __init__(self, system: str, backend: str | None = None) -> None:
        self.capabilities = SandboxCapabilities(
            backend=backend or f"{system.lower()}-native"
        )

    def prepare(
        self,
        command: list[str],
        deps: Any,
        runtime: RuntimeDirectories,
        *,
        allow_network: bool,
        publish_ports: list[int] | None = None,
    ) -> PreparedExecution:
        del allow_network, publish_ports
        mode = getattr(deps, "security_mode", SecurityMode.LIMITED)
        if mode in {SecurityMode.SAFE, SecurityMode.LIMITED}:
            raise PermissionError(
                f"execution sandbox unavailable on {platform.system()}; "
                "safe and limited modes refuse native execution"
            )
        return PreparedExecution(
            command=command,
            env=runtime_environment(runtime),
            profile_path=None,
            sandboxed=False,
            backend=self.capabilities.backend,
        )


class DockerSandbox:
    """Exécute les commandes dans un conteneur Linux jetable.

    Le même moteur sur les trois systèmes : le profil de sécurité s'écrit une
    fois et ne se rejoue pas par plateforme. Sur Windows et macOS, Docker place
    déjà les conteneurs Linux dans une machine virtuelle, ce qui ajoute une
    frontière au-dessus des namespaces.

    Le noyau reste partagé sur Linux natif. C'est une barrière solide contre un
    agent qui se trompe ou qu'on a détourné par injection ; ce n'en est pas une
    contre quelqu'un armé d'un exploit noyau. Les capacités le disent plutôt que
    de laisser croire à une isolation de machine virtuelle.
    """

    capabilities = SandboxCapabilities(
        backend="docker",
        process_tree_isolation=True,
        filesystem_isolation=True,
        network_isolation=True,
    )

    #: Image « tout compris » : Node, Python, Go, Rust, .NET, git. Elle pèse une
    #: dizaine de gigaoctets, téléchargés une fois — le prix d'un bac à sable qui
    #: sait exécuter les commandes de n'importe quel projet sans qu'on ait à
    #: changer d'image en cours de route. `AMK_SANDBOX_IMAGE` permet d'en choisir
    #: une plus légère, par exemple `node:22-bookworm` pour du web seul.
    default_image = "mcr.microsoft.com/devcontainers/universal:2-linux"

    def __init__(self, executable: str, image: str | None = None) -> None:
        self.executable = executable
        self.image = image or os.getenv("AMK_SANDBOX_IMAGE") or self.default_image

    @staticmethod
    def discover() -> str | None:
        executable, _ = DockerSandbox.status()
        return executable

    @staticmethod
    def status() -> tuple[str | None, str]:
        """Chemin du client et raison lisible quand il n'est pas exploitable.

        Un client installé ne suffit pas : le démon doit répondre. Docker Desktop
        peut être présent et arrêté, et prétendre à l'isolation dans ce cas
        ferait échouer chaque commande au lieu de retomber proprement sur le
        régime des autorisations.

        La raison est distinguée parce que les remèdes n'ont rien à voir :
        « absent du PATH » se règle en rouvrant un terminal, « démon muet » en
        démarrant Docker. Un diagnostic qui dit seulement « pas d'isolation »
        laisse chercher au mauvais endroit.
        """
        if os.getenv("AMK_DOCKER_SANDBOX", "1").casefold() in {"0", "false", "no", "off"}:
            return None, "désactivé par AMK_DOCKER_SANDBOX"
        executable = shutil.which("docker")
        if executable is None:
            return None, (
                "client `docker` introuvable dans le PATH — rouvrir le terminal "
                "après l'installation de Docker Desktop"
            )
        try:
            probe = subprocess.run(  # noqa: S603 - commande fixe, sans entrée utilisateur
                [executable, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"client `docker` injoignable : {type(exc).__name__}"
        if probe.returncode != 0 or not probe.stdout.strip():
            detail = (probe.stderr or probe.stdout).strip().splitlines()
            return None, (
                "démon Docker sans réponse — démarrer Docker Desktop"
                + (f" ({detail[0][:120]})" if detail else "")
            )
        return executable, f"docker {probe.stdout.strip()}"

    def image_available(self) -> tuple[bool, str]:
        """Check if the sandbox image is locally available.

        Returns (available, message). If the image is missing, the message
        explains how to pull it. This avoids cryptic "executable not found"
        errors when the real issue is a missing image.
        """
        try:
            probe = subprocess.run(  # noqa: S603 - commande fixe
                [self.executable, "image", "inspect", self.image],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"cannot inspect image: {type(exc).__name__}"
        if probe.returncode != 0:
            return False, (
                f"sandbox image not found: {self.image}\n"
                f"Pull it with: docker pull {self.image}\n"
                f"Or set AMK_SANDBOX_IMAGE to a lighter image (e.g. node:22-bookworm)"
            )
        return True, f"image {self.image} available"

    def prepare(
        self,
        command: list[str],
        deps: Any,
        runtime: RuntimeDirectories,
        *,
        allow_network: bool,
        publish_ports: list[int] | None = None,
    ) -> PreparedExecution:
        # Verify the image is available before building the command. Without this
        # check, Docker fails with cryptic "executable not found" errors when the
        # real issue is a missing or corrupted image.
        available, message = self.image_available()
        if not available:
            raise RuntimeError(
                f"Docker sandbox unavailable: {message}\n"
                f"The command was: {command}\n"
                f"To fix: pull the image or set AMK_SANDBOX_IMAGE to a valid image."
            )
        mode = getattr(deps, "security_mode", SecurityMode.LIMITED)
        # Le workspace est monté en lecture seule en mode `safe` : l'agent peut
        # inspecter et compiler, jamais modifier ce qu'il n'a pas le droit de
        # modifier.
        workspace_mode = "ro" if mode is SecurityMode.SAFE else "rw"
        arguments = [
            self.executable,
            "run",
            "--rm",
            "--interactive",
            # Ce que le conteneur ne peut pas faire, quoi qu'il exécute.
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=512",
            "--memory=2g",
            f"--network={'bridge' if allow_network else 'none'}",
            "--workdir=/workspace",
            f"--mount=type=bind,source={runtime.workspace},target=/workspace,"
            f"{'readonly' if workspace_mode == 'ro' else 'readonly=false'}",
            # Les artefacts doivent survivre au conteneur : ils sont lus par
            # l'interface après coup.
            f"--mount=type=bind,source={runtime.artifact_root},target=/artifacts",
            f"--mount=type=bind,source={runtime.temporary},target=/tmp",
            f"--mount=type=bind,source={runtime.cache},target=/cache",
        ]
        if allow_network and publish_ports:
            for port in publish_ports:
                arguments.extend(["--publish", f"{port}:{port}"])
        for key, value in sorted(_container_environment(runtime).items()):
            arguments.extend(["--env", f"{key}={value}"])
        arguments.append(self.image)
        arguments.extend(command)
        return PreparedExecution(
            command=arguments,
            env=runtime_environment(runtime),
            profile_path=None,
            sandboxed=True,
            backend=self.capabilities.backend,
        )


def _container_environment(runtime: RuntimeDirectories) -> dict[str, str]:
    """Environnement vu depuis l'intérieur, en chemins du conteneur.

    Réutiliser les chemins de l'hôte donnerait des variables qui ne désignent
    rien une fois franchie la frontière — et des outils qui écrivent leur cache
    dans un dossier créé au hasard, perdu à la fin du conteneur.
    """
    del runtime
    return {
        "HOME": "/tmp",  # noqa: S108 - chemin interne au conteneur éphémère
        "TMPDIR": "/tmp",  # noqa: S108 - chemin interne au conteneur éphémère
        "XDG_CACHE_HOME": "/cache",
        "NPM_CONFIG_CACHE": "/cache/npm",
        "PIP_CACHE_DIR": "/cache/pip",
        "AMK_ARTIFACTS": "/artifacts",
        "NO_COLOR": "1",
    }


class MacOSSeatbeltSandbox:
    capabilities = SandboxCapabilities(
        backend="macos-seatbelt",
        process_tree_isolation=True,
        filesystem_isolation=True,
        network_isolation=True,
    )

    @staticmethod
    def available() -> bool:
        return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None

    def prepare(
        self,
        command: list[str],
        deps: Any,
        runtime: RuntimeDirectories,
        *,
        allow_network: bool,
        publish_ports: list[int] | None = None,
    ) -> PreparedExecution:
        del deps, publish_ports
        protected = {
            runtime.events_root.parent / "providers.json",
            runtime.events_root.parent / "secrets.json",
            Path.home() / ".ssh",
            Path.home() / ".gnupg",
            Path.home() / ".aws",
            Path.home() / ".kube",
        }
        lines = [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process*)",
            "(allow sysctl-read)",
            "(allow file-read*)",
        ]
        writable = {runtime.workspace, runtime.runtime_root, runtime.artifact_root}
        for path in sorted(writable, key=str):
            lines.append(f'(allow file-write* (subpath "{_seatbelt(path)}"))')
        for path in sorted(protected, key=str):
            lines.append(f'(deny file-read* file-write* (subpath "{_seatbelt(path)}"))')
        if allow_network:
            lines.append("(allow network*)")

        fd, raw_path = tempfile.mkstemp(
            prefix="amk-seatbelt-",
            suffix=".sb",
            dir=runtime.runtime_root,
            text=True,
        )
        profile_path = Path(raw_path)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
        secure_file(profile_path)
        return PreparedExecution(
            command=["/usr/bin/sandbox-exec", "-f", str(profile_path), *command],
            env=runtime_environment(runtime),
            profile_path=profile_path,
            sandboxed=True,
            backend=self.capabilities.backend,
        )


def selected_backend() -> SandboxBackend:
    """Premier backend capable d'isoler réellement, sinon aucun.

    Docker passe devant : c'est le seul qui offre le même profil sur les trois
    systèmes, et le choix explicite du projet. Le seatbelt macOS reste derrière
    pour une machine sans démon Docker.
    """
    if docker := DockerSandbox.discover():
        return DockerSandbox(docker)
    if MacOSSeatbeltSandbox.available():
        return MacOSSeatbeltSandbox()
    return UnavailableSandbox(platform.system())


def sandbox_capabilities() -> SandboxCapabilities:
    return selected_backend().capabilities


def prepare_execution(
    command: list[str],
    deps: Any,
    *,
    allow_network: bool = False,
    publish_ports: list[int] | None = None,
) -> PreparedExecution:
    runtime = runtime_directories(deps)
    return selected_backend().prepare(
        command,
        deps,
        runtime,
        allow_network=allow_network,
        publish_ports=publish_ports,
    )


def runtime_directories(deps: Any) -> RuntimeDirectories:
    workspace = Path(deps.workspace).resolve()
    session_id = str(getattr(deps, "session_id", "standalone"))
    events_root = Path(deps.events.directory).resolve()
    runtime_root = events_root / "runtime" / session_id
    home = runtime_root / "home"
    temporary = runtime_root / "tmp"
    cache = runtime_root / "cache"
    artifact_root = events_root / "artifacts" / session_id
    for directory in (home, temporary, cache, artifact_root):
        directory.mkdir(parents=True, exist_ok=True)
    return RuntimeDirectories(
        workspace=workspace,
        events_root=events_root,
        runtime_root=runtime_root,
        home=home,
        temporary=temporary,
        cache=cache,
        artifact_root=artifact_root,
    )


def runtime_environment(runtime: RuntimeDirectories) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _SAFE_ENV or key.startswith("LC_")
    }
    env.update(
        {
            "HOME": str(runtime.home),
            "TMPDIR": str(runtime.temporary),
            "XDG_CACHE_HOME": str(runtime.cache),
            "NPM_CONFIG_CACHE": str(runtime.cache / "npm"),
            "PIP_CACHE_DIR": str(runtime.cache / "pip"),
            "NO_COLOR": "1",
        }
    )
    if os.name == "nt":
        drive, tail = os.path.splitdrive(str(runtime.home))
        env.update(
            {
                "USERPROFILE": str(runtime.home),
                "HOMEDRIVE": drive,
                "HOMEPATH": tail or "\\",
                "TEMP": str(runtime.temporary),
                "TMP": str(runtime.temporary),
                "LOCALAPPDATA": str(runtime.cache / "local"),
                "APPDATA": str(runtime.cache / "roaming"),
            }
        )
    return env


def _seatbelt(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


