#!/usr/bin/env python3
"""
Demeter GPU Arbiter — multiplexe LLM / ACE / Wan sur une seule RTX 3090 (24 Go).

Impossible de garder Qwen3-Coder (~21 Go) + ACE XL (~10 Go) en VRAM.
Ce service fait du time-sharing : un slot GPU actif à la fois, file d'attente,
libération auto après idle.

Priorité Steam (jeux) : si un jeu Steam/Proton utilise le GPU, l'arbitre
libère la VRAM (stop LLM/ACE/Wan) et refuse les acquire IA jusqu'à la fin du jeu.

API (port 8790):
  GET  /status
  POST /acquire   {"slot":"llm"|"ace"|"wan","owner":"cursor","timeout_s":600,"start":true}
                  start=false → libère la VRAM sans relancer la stack (Pinokio Start)
  POST /release   {"owner":"cursor"}  (optionnel)
  POST /touch     {"owner":"..."}     prolonge le lease

CLI: demeter-gpu status | use llm | use ace | use wan | release
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
HOST = os.environ.get("GPU_ARBITER_HOST", "0.0.0.0")
PORT = int(os.environ.get("GPU_ARBITER_PORT", "8790"))
PINOKIO = Path(os.environ.get("PINOKIO_HOME", "/mnt/ia/pinokio"))
REPO = Path(os.environ.get("DEMETER_REPO", str(Path.home() / "Documents/devforge")))
BOOT = REPO / "scripts" / "demeter-bootstrap"
LOG = Path(os.environ.get("GPU_ARBITER_LOG", "/mnt/ia/logs/gpu-arbiter.log"))
IDLE_RELEASE_S = int(os.environ.get("GPU_ARBITER_IDLE_S", "900"))  # 15 min
DEFAULT_SLOT = os.environ.get("GPU_ARBITER_DEFAULT", "llm")
# Dashboard Astro (build install Pinokio) — défaut: ../ui/dist depuis arbiter/
UI_DIST = Path(
    os.environ.get("GPU_ARBITER_UI_DIST")
    or os.environ.get("UI_DIST")
    or str(_SCRIPT_DIR.parent / "ui" / "dist")
).resolve()

# Steam / jeux = priorité absolue sur les slots IA
STEAM_PRIORITY = os.environ.get("GPU_ARBITER_STEAM_PRIORITY", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
STEAM_POLL_S = int(os.environ.get("GPU_ARBITER_STEAM_POLL_S", "5"))
STEAM_VRAM_MIN_MIB = int(os.environ.get("GPU_ARBITER_STEAM_VRAM_MIN_MIB", "400"))
STEAM_CLEAR_S = int(os.environ.get("GPU_ARBITER_STEAM_CLEAR_S", "30"))  # délai avant de rendre le GPU aux IA

SLOTS = ("llm", "ace", "wan", "steam", "idle")

# Processus Steam / Proton / overlay jeu
STEAM_NAME_RE = re.compile(
    r"(steam|steamwebhelper|steamsysinfo|steam\.exe|"
    r"proton|pressure-vessel|reaper|gamesoverlayui|"
    r"gamescope|wineserver|wine64-preloader|wine-preloader|"
    r"Origin\.exe|EADesktop|Battle\.net|EpicGames|GalaxyClient)",
    re.I,
)
# Processus IA / système à ignorer pour la détection « jeu »
AI_OR_SYS_RE = re.compile(
    r"(llama-server|acestep|python|tsx|node|litellm|Xorg|Xwayland|"
    r"gnome-shell|plasmashell|kwin|nvidia|nvcontainer|cuda|"
    r"demeter-gpu|pinokio)",
    re.I,
)

_lock = threading.RLock()
_state: dict[str, Any] = {
    "slot": "idle",
    "owner": None,
    "acquired_at": None,
    "last_touch": None,
    "switching": False,
    "last_error": None,
    "steam_active": False,
    "steam_detail": None,
    "steam_cleared_at": None,
}
_queue: list[dict[str, Any]] = []
_queue_cv = threading.Condition(_lock)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run(cmd: list[str] | str, timeout: int = 120) -> tuple[int, str]:
    if isinstance(cmd, str):
        shell = True
        args: Any = cmd
    else:
        shell = False
        args = cmd
    try:
        p = subprocess.run(
            args,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()
    except subprocess.TimeoutExpired as e:
        return 124, f"timeout: {e}"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def vram_used_mib() -> int | None:
    code, out = run(
        [
            "nvidia-smi",
            "--query-gpu=memory.used",
            "--format=csv,noheader,nounits",
        ],
        timeout=10,
    )
    if code != 0:
        return None
    try:
        return int(out.splitlines()[0].strip())
    except (ValueError, IndexError):
        return None


def pgrep(pattern: str) -> bool:
    code, _ = run(["pgrep", "-f", pattern], timeout=5)
    return code == 0


def gpu_compute_apps() -> list[dict[str, Any]]:
    """Liste des process utilisant la VRAM (nvidia-smi)."""
    code, out = run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        timeout=10,
    )
    if code != 0 or not out:
        return []
    apps: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            mem = int(float(parts[2]))
        except ValueError:
            continue
        apps.append({"pid": pid, "name": parts[1], "used_mib": mem})
    return apps


def steam_client_running() -> bool:
    return pgrep("/steam$|steam\\.sh|ubuntu12_32/steam|steamwebhelper")


def _iter_proc_cmdlines() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    try:
        for ent in Path("/proc").iterdir():
            if not ent.name.isdigit():
                continue
            try:
                raw = (ent / "cmdline").read_bytes()
            except OSError:
                continue
            if not raw:
                continue
            cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
            if cmd:
                out.append((int(ent.name), cmd))
    except OSError:
        pass
    return out


def detect_steam_priority() -> dict[str, Any] | None:
    """Jeu Steam/Proton réellement lancé (pas le client idle / steamwebhelper)."""
    if not STEAM_PRIORITY:
        return None

    reasons: list[str] = []
    game_apps: list[dict[str, Any]] = []

    for pid, cmd in _iter_proc_cmdlines():
        low = cmd.lower()
        if "steamwebhelper" in low or "srt-logger" in low or "oom_reaper" in low:
            continue
        if "steamapps/common" in low or "steamapps\\common" in low:
            reasons.append(f"steamapps/common pid={pid}")
            game_apps.append({"pid": pid, "name": cmd.split()[0][-80:], "used_mib": 0})
            continue
        if "compatdata" in low and (
            "proton" in low or "waitforexitandrun" in low or "reaper" in low
        ):
            reasons.append(f"compatdata/proton pid={pid}")
            game_apps.append({"pid": pid, "name": cmd.split()[0][-80:], "used_mib": 0})
            continue
        if re.search(r"/reaper(\s|$)", low) and "steam" in low:
            reasons.append(f"steam-reaper pid={pid}")
            game_apps.append({"pid": pid, "name": "reaper", "used_mib": 0})
            continue
        if "gameoverlayrenderer" in low or "gameoverlayui" in low:
            reasons.append(f"steam-overlay pid={pid}")

    apps = gpu_compute_apps()
    for a in apps:
        name = a["name"]
        if re.search(r"llama-server|acestep|kwin|Xorg|Xwayland", name, re.I):
            continue
        if a["used_mib"] < STEAM_VRAM_MIN_MIB:
            continue
        if steam_client_running() or reasons:
            if re.search(r"steamwebhelper|steam$", name, re.I) and a["used_mib"] < 1500:
                continue
            game_apps.append(a)
            reasons.append(f"vram:{name}:{a['used_mib']}MiB")

    if pgrep("gamescope") and (
        game_apps
        or any("steamapps" in r or "compatdata" in r or "vram:" in r for r in reasons)
    ):
        reasons.append("gamescope")

    if not reasons:
        return None

    return {
        "active": True,
        "reasons": reasons[:10],
        "apps": game_apps[:8],
        "client": steam_client_running(),
    }


def yield_to_steam(detail: dict[str, Any]) -> None:
    """Stoppe toute stack IA et marque le slot steam."""
    log(f"STEAM PRIORITY — free VRAM for game: {detail.get('reasons')}")
    stop_llm()
    stop_ace()
    stop_wan()
    _state["slot"] = "steam"
    _state["owner"] = "steam"
    _state["acquired_at"] = time.time()
    _state["last_touch"] = time.time()
    _state["steam_active"] = True
    _state["steam_detail"] = detail
    _state["steam_cleared_at"] = None
    _state["last_error"] = None
    # Annuler la file d'attente IA
    _queue.clear()
    _queue_cv.notify_all()


def stop_llm() -> None:
    log("stop llm (llama-server + optional unload via UI)")
    run("curl -sf -X POST http://127.0.0.1:1420/api/llm/stop >/dev/null 2>&1 || true", 15)
    time.sleep(1)
    run("pkill -f llama-server || true", 10)
    time.sleep(2)


def stop_ace() -> None:
    log("stop ace pipeline + studio server")
    run("pkill -f acestep.acestep_v15_pipeline || true", 10)
    run("pkill -f 'ace-step-studio.pinokio/app/app/server' || true", 10)
    run("pkill -f 'tsx src/index.ts' || true", 10)
    time.sleep(3)


def stop_wan() -> None:
    log("stop wan")
    run("pkill -f 'wan.*gradio|Wan2GP|SERVER_PORT=8188' || true", 10)
    # Pinokio wan often leaves python on 8188
    run("fuser -k 8188/tcp 2>/dev/null || true", 10)
    time.sleep(2)


def start_llm() -> None:
    log("start llm")
    u = PINOKIO / "api/uncensored-local-studio/app"
    serve = u / "scripts/server/serve.cjs"
    if serve.exists() and not pgrep("scripts/server/serve.cjs"):
        env = os.environ.copy()
        env["FRONTEND_PORT"] = os.environ.get("UNCENSORED_UI_PORT", "1420")
        env["LLM_PORT"] = os.environ.get("LLM_PORT", "10086")
        env["LLM_HOST"] = "0.0.0.0"
        log_dir = Path("/mnt/ia/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "uncensored-serve.log").open("a") as lf:
            subprocess.Popen(
                ["node", "scripts/server/serve.cjs"],
                cwd=str(u),
                env=env,
                stdout=lf,
                stderr=lf,
                start_new_session=True,
            )
        time.sleep(4)
    load = BOOT / "load-demeter-llm-gpu.sh"
    if load.exists():
        run(["bash", str(load)], timeout=180)
    # wait ready
    for _ in range(90):
        if pgrep("llama-server"):
            try:
                urllib.request.urlopen("http://127.0.0.1:10086/v1/models", timeout=2)
                log("llm ready on :10086")
                return
            except Exception:  # noqa: BLE001
                pass
        time.sleep(2)
    log("WARN llm start incomplete")


def start_ace() -> None:
    log("start ace")
    script = BOOT / "start-ace-step-studio-pinokio.sh"
    if not script.exists():
        # fallback copied to /tmp during ops
        script = Path("/tmp/start-ace-step-studio-pinokio.sh")
    if script.exists():
        env = os.environ.copy()
        env["ARBITER_SKIP_ACQUIRE"] = "1"  # avoid re-entrant acquire
        try:
            p = subprocess.run(
                ["bash", str(script)],
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            if out:
                log(out[-2000:])
        except Exception as e:  # noqa: BLE001
            log(f"start_ace error: {e}")
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:8001/", timeout=2)
            log("ace ready on :8001")
            return
        except Exception:  # noqa: BLE001
            time.sleep(2)
    log("WARN ace start incomplete")


def start_wan() -> None:
    log("start wan — démarrer via Pinokio UI si pas d'autostart script")
    # Best-effort: look for start helper
    helper = BOOT / "start-wan.sh"
    if helper.exists():
        run(["bash", str(helper)], timeout=180)
    else:
        log("WARN: pas de start-wan.sh — utiliser Pinokio → Wan → Start après acquire")


def switch_to(slot: str, start: bool = True) -> None:
    """Free conflicting GPU holders, optionally start the target stack.

    start=False : Pinokio Start button — free VRAM only; Pinokio launches ACE itself.
    """
    if slot == "steam":
        # steam = yield only (on ne lance pas Steam)
        det = detect_steam_priority() or {"reasons": ["manual"], "apps": [], "client": steam_client_running()}
        yield_to_steam(det)
        return

    current = _state["slot"]
    if current == slot and not _state["switching"]:
        if slot == "llm" and pgrep("llama-server"):
            return
        if slot == "ace" and (not start or pgrep("acestep.acestep_v15_pipeline")):
            # prepare_only: still ensure LLM is stopped
            if not start and pgrep("llama-server"):
                pass  # fall through to stop
            else:
                return
        if slot == "idle":
            return

    _state["switching"] = True
    _state["last_error"] = None
    try:
        log(f"switch {current} → {slot} (start={start})")
        if slot != "llm":
            stop_llm()
        if slot != "ace":
            stop_ace()
        if slot != "wan":
            stop_wan()

        if start:
            if slot == "llm":
                start_llm()
            elif slot == "ace":
                start_ace()
            elif slot == "wan":
                start_wan()
        # idle / prepare_only: everything conflicting stopped

        _state["slot"] = slot
        if slot != "steam":
            _state["steam_active"] = False
        log(f"active slot={slot} vram={vram_used_mib()} MiB")
    except Exception as e:  # noqa: BLE001
        _state["last_error"] = str(e)
        log(f"ERROR switch: {e}")
        raise
    finally:
        _state["switching"] = False


def status_payload() -> dict[str, Any]:
    steam = detect_steam_priority()
    return {
        "ok": True,
        "slot": _state["slot"],
        "owner": _state["owner"],
        "acquired_at": _state["acquired_at"],
        "last_touch": _state["last_touch"],
        "switching": _state["switching"],
        "last_error": _state["last_error"],
        "queue": [
            {
                "slot": q["slot"],
                "owner": q["owner"],
                "waited_s": int(time.time() - q["enqueued_at"]),
            }
            for q in _queue
        ],
        "vram_used_mib": vram_used_mib(),
        "procs": {
            "llama": pgrep("llama-server"),
            "ace": pgrep("acestep.acestep_v15_pipeline"),
            "litellm": pgrep("litellm --config"),
            "steam_client": steam_client_running(),
            "steam_game": bool(steam),
        },
        "steam": {
            "priority_enabled": STEAM_PRIORITY,
            "active": bool(steam) or _state.get("slot") == "steam",
            "detail": steam or _state.get("steam_detail"),
        },
        "idle_release_s": IDLE_RELEASE_S,
        "hint": "Steam/jeux prioritaires. POST /acquire {\"slot\":\"llm|ace|wan\"} — un seul modèle GPU à la fois",
    }


def acquire(slot: str, owner: str, timeout_s: float, start: bool = True) -> dict[str, Any]:
    if slot not in ("llm", "ace", "wan"):
        return {"ok": False, "error": f"slot invalide: {slot}"}

    # Steam bloque immédiatement les demandes IA
    steam = detect_steam_priority()
    if steam:
        with _queue_cv:
            if _state["slot"] != "steam":
                try:
                    yield_to_steam(steam)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": str(e), **status_payload()}
        return {
            "ok": False,
            "error": "steam_priority",
            "message": "Un jeu Steam/Proton utilise le GPU — IA en pause. "
            "Réessaie quand le jeu est fermé.",
            "steam": steam,
            **status_payload(),
        }

    deadline = time.time() + timeout_s
    ticket = {
        "slot": slot,
        "owner": owner,
        "start": start,
        "enqueued_at": time.time(),
        "event": threading.Event(),
    }

    with _queue_cv:
        # Fast path: already ours — but prepare_only must still free LLM if present
        if (
            _state["slot"] == slot
            and not _state["switching"]
            and (_state["owner"] in (None, owner))
            and (start or not pgrep("llama-server"))
        ):
            _state["owner"] = owner
            _state["last_touch"] = time.time()
            if _state["acquired_at"] is None:
                _state["acquired_at"] = time.time()
            return {
                "ok": True,
                "slot": slot,
                "owner": owner,
                "queued": False,
                "start": start,
                **status_payload(),
            }

        _queue.append(ticket)
        _queue_cv.notify_all()

    while time.time() < deadline:
        with _queue_cv:
            # Am I next? Preempt current slot (single-user Demeter — swap immédiat)
            if _queue and _queue[0] is ticket and not _state["switching"]:
                try:
                    switch_to(slot, start=bool(ticket.get("start", True)))
                    _state["owner"] = owner
                    _state["acquired_at"] = time.time()
                    _state["last_touch"] = time.time()
                    _queue.pop(0)
                    _queue_cv.notify_all()
                    return {
                        "ok": True,
                        "slot": slot,
                        "owner": owner,
                        "queued": True,
                        "start": start,
                        **status_payload(),
                    }
                except Exception as e:  # noqa: BLE001
                    if ticket in _queue:
                        _queue.remove(ticket)
                    return {"ok": False, "error": str(e), **status_payload()}
            # Same slot already active
            if (
                _state["slot"] == slot
                and not _state["switching"]
                and _queue
                and _queue[0] is ticket
                and (start or not pgrep("llama-server"))
            ):
                _state["owner"] = owner
                _state["last_touch"] = time.time()
                _queue.pop(0)
                _queue_cv.notify_all()
                return {
                    "ok": True,
                    "slot": slot,
                    "owner": owner,
                    "queued": False,
                    "start": start,
                    **status_payload(),
                }
        time.sleep(0.5)

    with _queue_cv:
        if ticket in _queue:
            _queue.remove(ticket)
            _queue_cv.notify_all()
    return {"ok": False, "error": "timeout waiting for GPU slot", "queue": status_payload()["queue"]}


def release(owner: str | None = None) -> dict[str, Any]:
    with _queue_cv:
        if owner and _state["owner"] and _state["owner"] != owner:
            return {"ok": False, "error": f"owned by {_state['owner']}", **status_payload()}
        _state["owner"] = None
        _state["last_touch"] = time.time()
        _queue_cv.notify_all()
        return {"ok": True, "released": True, **status_payload()}


def touch(owner: str | None = None) -> dict[str, Any]:
    with _queue_cv:
        if owner and _state["owner"] and _state["owner"] != owner:
            return {"ok": False, "error": "not owner"}
        _state["last_touch"] = time.time()
        return {"ok": True, **status_payload()}


def idle_watcher() -> None:
    while True:
        time.sleep(30)
        with _queue_cv:
            if _queue or _state["switching"] or _state["slot"] in ("idle", "steam"):
                continue
            if detect_steam_priority():
                continue
            last = _state["last_touch"] or _state["acquired_at"]
            if last is None:
                continue
            if time.time() - last < IDLE_RELEASE_S:
                continue
            # Prefer returning to default LLM for agents if queue empty
            target = DEFAULT_SLOT if DEFAULT_SLOT in ("llm", "ace", "wan") else "idle"
            if _state["slot"] == target:
                _state["owner"] = None
                continue
            log(f"idle {IDLE_RELEASE_S}s → switch to {target}")
            try:
                switch_to(target)
                _state["owner"] = None
                _state["acquired_at"] = time.time()
                _state["last_touch"] = time.time()
            except Exception as e:  # noqa: BLE001
                log(f"idle switch error: {e}")
            _queue_cv.notify_all()


def steam_watcher() -> None:
    """Surveille Steam/jeux : priorité GPU absolue, puis rendu aux IA après CLEAR_S."""
    if not STEAM_PRIORITY:
        log("Steam priority disabled (GPU_ARBITER_STEAM_PRIORITY=0)")
        return
    log(
        f"Steam priority ON poll={STEAM_POLL_S}s vram_min={STEAM_VRAM_MIN_MIB}MiB "
        f"clear={STEAM_CLEAR_S}s"
    )
    while True:
        time.sleep(STEAM_POLL_S)
        try:
            steam = detect_steam_priority()
            with _queue_cv:
                if _state["switching"]:
                    continue
                if steam:
                    _state["steam_cleared_at"] = None
                    if _state["slot"] != "steam" or pgrep("llama-server") or pgrep(
                        "acestep.acestep_v15_pipeline"
                    ):
                        try:
                            yield_to_steam(steam)
                        except Exception as e:  # noqa: BLE001
                            log(f"steam yield error: {e}")
                    else:
                        _state["steam_active"] = True
                        _state["steam_detail"] = steam
                        _state["last_touch"] = time.time()
                    continue

                # Plus de jeu
                if _state["slot"] == "steam" or _state.get("steam_active"):
                    if _state["steam_cleared_at"] is None:
                        _state["steam_cleared_at"] = time.time()
                        log(f"Steam cleared — wait {STEAM_CLEAR_S}s before restoring AI")
                        continue
                    if time.time() - float(_state["steam_cleared_at"]) < STEAM_CLEAR_S:
                        continue
                    target = DEFAULT_SLOT if DEFAULT_SLOT in ("llm", "ace", "wan") else "idle"
                    log(f"Steam gone → restore slot {target}")
                    _state["steam_active"] = False
                    _state["steam_detail"] = None
                    _state["steam_cleared_at"] = None
                    try:
                        switch_to(target, start=True)
                        _state["owner"] = None
                        _state["acquired_at"] = time.time()
                        _state["last_touch"] = time.time()
                    except Exception as e:  # noqa: BLE001
                        log(f"steam restore error: {e}")
                    _queue_cv.notify_all()
        except Exception as e:  # noqa: BLE001
            log(f"steam_watcher error: {e}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        log("http " + (fmt % args))

    def _json(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _request_path(self) -> str:
        raw = urllib.parse.urlparse(self.path).path or "/"
        return urllib.parse.unquote(raw)

    def _serve_file(self, file_path: Path, code: int = 200) -> bool:
        try:
            data = file_path.read_bytes()
        except OSError:
            return False
        ctype, _ = mimetypes.guess_type(str(file_path))
        if not ctype:
            if file_path.suffix == ".js":
                ctype = "application/javascript"
            elif file_path.suffix == ".css":
                ctype = "text/css"
            elif file_path.suffix == ".svg":
                ctype = "image/svg+xml"
            else:
                ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
            "application/javascript",
            "application/json",
            "image/svg+xml",
        ):
            ctype = f"{ctype}; charset=utf-8"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)
        return True

    def _serve_ui(self) -> None:
        req = self._request_path()
        if not UI_DIST.is_dir():
            self._json(
                503,
                {
                    "ok": False,
                    "error": "ui_not_built",
                    "message": f"UI absente ({UI_DIST}). Lance install.js (npm run build).",
                    "hint": "API OK — GET /status",
                },
            )
            return

        # Normalize + prevent path traversal
        rel = req.lstrip("/") or "index.html"
        candidate = (UI_DIST / rel).resolve()
        try:
            candidate.relative_to(UI_DIST)
        except ValueError:
            self._json(403, {"ok": False, "error": "forbidden"})
            return

        if candidate.is_file():
            self._serve_file(candidate)
            return

        # SPA / Astro fallback
        index = UI_DIST / "index.html"
        if index.is_file():
            self._serve_file(index)
            return

        self._json(404, {"ok": False, "error": "ui_index_missing", "path": str(UI_DIST)})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self._request_path()
        if path.startswith("/status"):
            with _lock:
                self._json(200, status_payload())
            return
        if path.startswith("/health"):
            self._json(200, {"ok": True})
            return
        # Dashboard Astro (même origine que l'API)
        self._serve_ui()

    def do_POST(self) -> None:  # noqa: N802
        path = self._request_path()
        body = self._read_json()
        if path.startswith("/acquire"):
            slot = str(body.get("slot") or "").strip()
            owner = str(body.get("owner") or "anonymous").strip() or "anonymous"
            timeout_s = float(body.get("timeout_s") or 600)
            start = body.get("start", True)
            if isinstance(start, str):
                start = start.strip().lower() not in ("0", "false", "no")
            result = acquire(slot, owner, timeout_s, start=bool(start))
            self._json(200 if result.get("ok") else 409, result)
            return
        if path.startswith("/release"):
            owner = body.get("owner")
            result = release(str(owner) if owner else None)
            self._json(200 if result.get("ok") else 409, result)
            return
        if path.startswith("/touch"):
            owner = body.get("owner")
            result = touch(str(owner) if owner else None)
            self._json(200 if result.get("ok") else 409, result)
            return
        self._json(404, {"ok": False, "error": "not found"})


def main() -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ui_note = f"Serving UI from {UI_DIST}" if UI_DIST.is_dir() else f"UI not built yet ({UI_DIST})"
    log(f"Demeter GPU Arbiter on {HOST}:{PORT} pinokio={PINOKIO} — {ui_note}")
    threading.Thread(target=idle_watcher, name="idle-watcher", daemon=True).start()
    threading.Thread(target=steam_watcher, name="steam-watcher", daemon=True).start()

    # Detect current occupancy
    with _lock:
        steam = detect_steam_priority()
        if steam:
            yield_to_steam(steam)
        elif pgrep("llama-server"):
            _state["slot"] = "llm"
        elif pgrep("acestep.acestep_v15_pipeline"):
            _state["slot"] = "ace"
        _state["last_touch"] = time.time()

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)

    def shutdown(*_a: Any) -> None:
        log("shutdown")
        httpd.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
