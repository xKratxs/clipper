"""Version para GitHub Actions: procesa un video y publica cada clip en un Release en cuanto termina.

El estado del trabajo (progreso, clips, errores) se guarda dentro de la descripcion del Release,
asi la galeria (docs/index.html) puede mostrarlo sin servidor.

Uso (lo llama el workflow .github/workflows/clips.yml):
    python -m app.cli run        # lee las opciones de variables de entorno CLIP_*
    python -m app.cli fail MSG   # marca el trabajo como fallido si se corto
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import pipeline

GH = shutil.which("gh") or "gh"
TAG = f"clips-{os.environ.get('GITHUB_RUN_ID', 'local')}"
RUN_URL = "{}/{}/actions/runs/{}".format(
    os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
    os.environ.get("GITHUB_REPOSITORY", ""),
    os.environ.get("GITHUB_RUN_ID", ""),
)
STATUS = {"running": "⏳ Procesando", "done": "✅ Terminado", "error": "❌ Error"}
STATE_RE = re.compile(r"<!--clipper\n(.*?)\n-->", re.S)


def gh(*args: str, stdin: str | None = None) -> str:
    r = subprocess.run([GH, *args], input=stdin, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} fallo: {r.stderr.strip()[-500:]}")
    return r.stdout


def _fmt(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def render_body(state: dict) -> str:
    lines = [
        f"**Estado:** {STATUS.get(state['status'], state['status'])} — {state['message']}",
        f"**Video:** {state['url']}",
        f"**Clips:** {len(state['clips'])}/{state['total']}",
        "",
    ]
    for c in state["clips"]:
        lines.append(f"- **{c['title']}** · {round(c['score'])}/100 · {_fmt(c['start'])}–{_fmt(c['end'])}")
    data = json.dumps(state, ensure_ascii=False).replace(">", "\\" + "u003e")  # nunca cierra el comentario
    return "\n".join(lines) + f"\n\n<!--clipper\n{data}\n-->\n"


class Release:
    def __init__(self, state: dict):
        self.state = state
        self._last = 0.0

    def create(self):
        gh("release", "create", TAG, "--title", "Procesando video...", "--notes-file", "-",
           "--prerelease", stdin=render_body(self.state))

    def push(self, force: bool = False):
        if not force and time.time() - self._last < 15:
            return
        self._last = time.time()
        try:
            gh("release", "edit", TAG, "--notes-file", "-", stdin=render_body(self.state))
        except RuntimeError as e:  # un fallo al actualizar el progreso no debe parar el trabajo
            print(f"[aviso] {e}", flush=True)

    def title(self, title: str):
        gh("release", "edit", TAG, "--title", title[:120] or "Video")

    def upload(self, *files: Path):
        gh("release", "upload", TAG, *map(str, files), "--clobber")

    def finish(self):
        gh("release", "edit", TAG, "--prerelease=false", "--notes-file", "-", stdin=render_body(self.state))


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except ValueError:
        return default


def _hex_to_ass(color: str) -> str:
    c = color.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
        return "&H0000E5FF"
    return f"&H00{c[4:6]}{c[2:4]}{c[0:2]}".upper()


def run() -> int:
    url = os.environ.get("CLIP_URL", "").strip()
    n = _env_int("CLIP_NUM", 5, 1, 30)
    min_len, max_len = sorted((_env_int("CLIP_MIN", 20, 5, 180), _env_int("CLIP_MAX", 60, 10, 300)))
    vertical = os.environ.get("CLIP_FORMAT", "vertical") != "horizontal"
    model = os.environ.get("CLIP_MODEL", "small")
    if model not in ("tiny", "base", "small", "medium", "large-v3", "turbo"):
        model = "small"
    language = os.environ.get("CLIP_LANG", "").strip() or None
    style = {"highlight": _hex_to_ass(os.environ.get("CLIP_COLOR", "#FFE500"))}

    state = {
        "v": 1, "status": "running", "progress": 0.0, "message": "Empezando...", "url": url,
        "title": "", "total": n, "clips": [], "method": None, "run_url": RUN_URL,
        "vertical": vertical, "started_at": time.time(),
    }
    rel = Release(state)
    rel.create()

    def progress(p: float, msg: str):
        state["progress"] = round(min(p, 1.0), 3)
        state["message"] = msg
        print(f"[{p:5.1%}] {msg}", flush=True)
        rel.push()

    work = Path("work")
    work.mkdir(exist_ok=True)
    try:
        if "youtu" not in url:
            raise RuntimeError("El link no parece de YouTube.")
        info = pipeline.download_video(url, work, progress)
        state["title"] = info["title"]
        rel.title(info["title"])
        tr = pipeline.transcribe(info["path"], model, language, info["duration"], progress)
        if not tr["segments"]:
            raise RuntimeError("No se detecto voz en el video.")
        clips, method = pipeline.select_clips(tr["segments"], info["title"], n, min_len, max_len,
                                              info["duration"], progress)
        if not clips:
            raise RuntimeError("No se encontraron clips con esa duracion. Prueba otro rango.")
        state["method"] = method
        state["total"] = len(clips)
        for i, clip in enumerate(clips, 1):
            progress(0.75 + 0.25 * (i - 1) / len(clips), f"Renderizando clip {i} de {len(clips)}...")
            res = pipeline.render_clip(info["path"], clip, work, i, vertical, style)
            poster = work / f"clip_{i:02d}.jpg"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", str(work / res["video"]),
                            "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(poster)], check=False)
            files = [work / res["video"], work / res["srt"]]
            if poster.exists():
                files.append(poster)
                res["poster"] = poster.name
            rel.upload(*files)
            state["clips"].append(res)
            rel.push(force=True)
        state.update(status="done", progress=1.0, message="Listo")
        rel.finish()
        return 0
    except Exception as e:
        traceback.print_exc()
        state.update(status="error", message=str(e)[:400])
        rel.push(force=True)
        return 1


def fail(message: str) -> int:
    """Lo ejecuta el workflow si el paso principal se corto (cancelado, tiempo agotado...)."""
    try:
        body = json.loads(gh("release", "view", TAG, "--json", "body"))["body"]
    except Exception:
        return 0
    m = STATE_RE.search(body)
    if not m:
        return 0
    state = json.loads(m.group(1))
    if state.get("status") == "running":
        state.update(status="error", message=message)
        gh("release", "edit", TAG, "--notes-file", "-", stdin=render_body(state))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "fail":
        sys.exit(fail(sys.argv[2] if len(sys.argv) > 2 else "El proceso se detuvo."))
    sys.exit(run())
