from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .errors import ConfigurationError


def listening_pids(port: int) -> list[int]:
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        check=False,
    )
    return sorted({int(line) for line in result.stdout.splitlines() if line.isdigit()})


def process_command(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def process_cwd(pid: int) -> Path | None:
    result = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        if line.startswith("n/"):
            return Path(line[1:]).resolve()
    return None


def is_amk_process(pid: int, root: Path, port: int, api_port: int, web_port: int) -> bool:
    command = process_command(pid)
    cwd = process_cwd(pid)
    if port == api_port:
        return cwd == root and "amk serve" in command
    if port == web_port:
        return cwd == root / "surfaces" / "web" and any(
            marker in command for marker in ("vinext dev", "npm run dev")
        )
    return False


def stop_previous_instances(root: Path, api_port: int, web_port: int) -> list[int]:
    owned: list[int] = []
    conflicts: list[tuple[int, int, str]] = []
    for port in (api_port, web_port):
        for pid in listening_pids(port):
            if pid == os.getpid():
                continue
            if is_amk_process(pid, root, port, api_port, web_port):
                owned.append(pid)
            else:
                conflicts.append((port, pid, process_command(pid)))
    if conflicts:
        details = "; ".join(
            f"port {port}: PID {pid} ({command or 'unknown'})" for port, pid, command in conflicts
        )
        raise ConfigurationError(f"refusing to stop non-AMK process(es): {details}")
    for pid in owned:
        _terminate(pid)
    return owned


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
        if port == preferred and all(
            is_amk_process(pid, root, port, api_port, preferred) for pid in pids
        ):
            return port
    raise ConfigurationError(
        f"no free web port found between {preferred} and {preferred + attempts - 1}"
    )


def _terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_web(root: Path, host: str, api_port: int, web_port: int) -> int:
    root = root.resolve()
    # The index is derived data. Rebuild it before starting the long-lived
    # scheduler so a newly installed or edited module cannot poison cron runs
    # with a stale catalog from a previous process.
    from .modules import ModuleRegistry

    ModuleRegistry(root / "tools").build_index()
    web_root = root / "surfaces" / "web"
    if not (web_root / "package.json").is_file():
        raise ConfigurationError(f"missing web surface: {web_root}")
    npm = shutil.which("npm")
    if npm is None:
        raise ConfigurationError("npm is required to launch the web surface")
    amk = shutil.which("amk") or str(Path(sys.executable).parent / "amk")
    selected_web_port = available_web_port(root, web_port, api_port)
    if selected_web_port != web_port:
        print(
            f"Port {web_port} is used by another application; "
            f"AMK Web will use port {selected_web_port}."
        )
        web_port = selected_web_port
    stopped = stop_previous_instances(root, api_port, web_port)
    if stopped:
        print(f"Stopped previous AMK instance(s): {', '.join(map(str, stopped))}")

    backend = subprocess.Popen(
        [amk, "serve", "--host", host, "--port", str(api_port)],
        cwd=root,
        start_new_session=True,
    )
    environment = {**os.environ, "AMK_KERNEL_URL": f"http://{host}:{api_port}"}
    frontend = subprocess.Popen(
        [npm, "run", "dev", "--", "--host", host, "--port", str(web_port)],
        cwd=web_root,
        env=environment,
        start_new_session=True,
    )
    print(f"AMK API: http://{host}:{api_port}")
    print(f"AMK Web: http://{host}:{web_port}")
    try:
        while True:
            backend_status = backend.poll()
            frontend_status = frontend.poll()
            if backend_status is not None:
                return backend_status
            if frontend_status is not None:
                return frontend_status
            time.sleep(0.25)
    except KeyboardInterrupt:
        return 0
    finally:
        _stop_group(frontend)
        _stop_group(backend)


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=4)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
