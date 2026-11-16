"""Pipeline: descargar video de YouTube -> transcribir -> elegir mejores clips -> renderizar con subtitulos."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ProgressFn = Callable[[float, str], None]


# --------------------------------------------------------------------------- #
# 1. Descarga
# --------------------------------------------------------------------------- #
def find_source(workdir: Path) -> Path | None:
    for p in workdir.glob("source.*"):
        if p.suffix in (".mp4", ".mkv", ".webm", ".mov"):
            return p
    return None


def download_video(url: str, workdir: Path, progress: ProgressFn) -> dict:
    import yt_dlp

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total:
                progress(0.02 + 0.18 * (done / total), "Descargando video...")

    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    # En servidores en la nube YouTube suele pedir "confirma que no eres un bot": se resuelve con cookies.
    cookies = os.environ.get("YTDLP_COOKIES")
    if cookies and Path(cookies).is_file():
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    path = find_source(workdir)
    return {
        "path": path,
        "title": info.get("title") or "video",
        "duration": float(info.get("duration") or 0),
    }


# --------------------------------------------------------------------------- #
# 2. Transcripcion (faster-whisper, local)
# --------------------------------------------------------------------------- #
_MODELS: dict[str, object] = {}


def _get_model(size: str):
    if size not in _MODELS:
        import ctranslate2
        from faster_whisper import WhisperModel

        if ctranslate2.get_cuda_device_count() > 0:
            _MODELS[size] = WhisperModel(size, device="cuda", compute_type="float16")
        else:
            _MODELS[size] = WhisperModel(size, device="cpu", compute_type="int8")
    return _MODELS[size]


def _load_audio(path: Path):
    """Decodifica el audio a 16 kHz mono float32 con ffmpeg (evita depender de PyAV)."""
    import numpy as np

    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
         "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(out, np.float32).copy()


def transcribe(path: Path, model_size: str, language: str | None, duration: float,
               progress: ProgressFn) -> dict:
    progress(0.21, "Extrayendo audio...")
    audio = _load_audio(path)
    progress(0.22, f"Cargando modelo de transcripcion ({model_size})...")
    model = _get_model(model_size)
    segments_iter, info = model.transcribe(
        audio,
        language=language or None,
        word_timestamps=True,
        vad_filter=True,
    )
    total = duration or info.duration or 1
    segments = []
    for seg in segments_iter:
        words = [
            {"start": w.start, "end": w.end, "word": w.word.strip()}
            for w in (seg.words or []) if w.word.strip()
        ]
        segments.append({"start": seg.start, "end": seg.end, "text": seg.text.strip(), "words": words})
        progress(0.22 + 0.48 * min(seg.end / total, 1), "Transcribiendo audio...")
    return {"language": info.language, "segments": segments}


# --------------------------------------------------------------------------- #
# 3. Seleccion de clips
# --------------------------------------------------------------------------- #
@dataclass
class Clip:
    start: float
    end: float
    title: str
    reason: str = ""
    score: float = 0.0
    words: list = field(default_factory=list)


CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["start", "end", "title", "reason", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


def _select_with_claude(segments, title, n, min_len, max_len) -> list[Clip]:
    import anthropic

    lines = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments)
    prompt = (
        f"Eres editor de video para TikTok/Reels/Shorts. Video: \"{title}\".\n"
        f"Abajo esta la transcripcion con marcas de tiempo en segundos.\n\n"
        f"Elige los {n} mejores momentos para clips virales. Cada clip debe:\n"
        f"- durar entre {min_len} y {max_len} segundos\n"
        f"- empezar al inicio de una frase con un gancho fuerte y terminar con la idea cerrada\n"
        f"- entenderse solo, sin contexto del resto del video\n"
        f"- no solaparse con otros clips\n"
        f"Usa como start/end los tiempos exactos de los segmentos. 'title' es un titulo corto y llamativo "
        f"en el idioma del video, 'reason' explica en una frase por que funciona, 'score' de 0 a 100.\n\n"
        f"<transcripcion>\n{lines}\n</transcripcion>"
    )
    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=os.environ.get("CLIPS_MODEL", "claude-opus-5-5"),
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": CLIP_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"Claude no devolvio clips (stop_reason={resp.stop_reason})")
    text = "".join(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    return [Clip(c["start"], c["end"], c["title"], c["reason"], c["score"]) for c in data["clips"]]


HOOK_WORDS = re.compile(
    r"\b(nunca|siempre|secreto|increible|importante|error|verdad|mejor|peor|nadie|todos|"
    r"dinero|miedo|imagina|mira|escucha|por que|cuidado|wow|never|always|secret|crazy|"
    r"important|mistake|truth|best|worst|nobody|everyone|money|imagine|listen|why)\b",
    re.I,
)


def _select_heuristic(segments, n, min_len, max_len) -> list[Clip]:
    """Sin API key: puntua ventanas por densidad de habla, preguntas/exclamaciones y palabras gancho."""
    target = (min_len + max_len) / 2
    candidates = []
    for i, first in enumerate(segments):
        text, j = [], i
        while j < len(segments) and segments[j]["end"] - first["start"] <= max_len:
            text.append(segments[j]["text"])
            dur = segments[j]["end"] - first["start"]
            if dur >= min_len:
                joined = " ".join(text)
                words = len(joined.split())
                score = (
                    words / dur * 10
                    + joined.count("?") * 4 + joined.count("!") * 4
                    + len(HOOK_WORDS.findall(joined)) * 6
                    + (8 if HOOK_WORDS.search(segments[i]["text"]) or "?" in segments[i]["text"] else 0)
                    - abs(dur - target) * 0.3
                )
                candidates.append(Clip(first["start"], segments[j]["end"],
                                       segments[i]["text"][:60], "Seleccion automatica", score))
            j += 1
    candidates.sort(key=lambda c: c.score, reverse=True)
    chosen: list[Clip] = []
    for c in candidates:
        if all(c.end <= o.start or c.start >= o.end for o in chosen):
            chosen.append(c)
        if len(chosen) == n:
            break
    if chosen:
        top = max(c.score for c in chosen) or 1
        for c in chosen:
            c.score = round(max(c.score, 0) / top * 100)
    return chosen


def _snap(clips: list[Clip], segments, min_len, max_len, duration) -> list[Clip]:
    """Ajusta los limites a frases completas y respeta la duracion pedida."""
    out = []
    for c in clips:
        starts = [s["start"] for s in segments]
        ends = [s["end"] for s in segments]
        c.start = min(starts, key=lambda t: abs(t - c.start)) if starts else c.start
        c.end = min(ends, key=lambda t: abs(t - c.end)) if ends else c.end
        if c.end - c.start > max_len + 5:
            c.end = c.start + max_len
        if c.end - c.start < min_len * 0.6:
            continue
        c.start = max(0.0, c.start - 0.15)
        c.end = min(duration or c.end + 0.3, c.end + 0.3)
        c.words = [w for s in segments for w in s["words"] if w["start"] >= c.start - 0.05 and w["end"] <= c.end + 0.05]
        out.append(c)
    return out


def select_clips(segments, title, n, min_len, max_len, duration, progress: ProgressFn) -> tuple[list[Clip], str]:
    progress(0.72, "Buscando los mejores momentos...")
    method = "heuristica"
    clips: list[Clip] = []
    has_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if has_key and segments:
        try:
            clips = _select_with_claude(segments, title, n, min_len, max_len)
            method = "claude"
        except Exception as e:  # cae a la heuristica si falla la API
            print(f"[clips] Claude fallo, uso heuristica: {e}")
    if not clips:
        clips = _select_heuristic(segments, n, min_len, max_len)
    clips = _snap(clips, segments, min_len, max_len, duration)
    clips.sort(key=lambda c: c.score, reverse=True)
    return clips[:n], method


# --------------------------------------------------------------------------- #
# 4. Subtitulos + render con ffmpeg
# --------------------------------------------------------------------------- #
def _ass_time(t: float) -> str:
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _srt_time(t: float) -> str:
    t = max(t, 0)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _chunks(words, size=3):
    for i in range(0, len(words), size):
        yield words[i:i + size]


def _clean(w: str) -> str:
    return w.replace("{", "").replace("}", "").replace("\\", "")


def write_subtitles(clip: Clip, ass_path: Path, srt_path: Path, vertical: bool, style: dict):
    w, h = (1080, 1920) if vertical else (1920, 1080)
    font_size = style.get("size", 84 if vertical else 64)
    margin_v = int(h * (0.28 if vertical else 0.08))
    highlight = style.get("highlight", "&H0000E5FF")  # amarillo (BGR)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{os.environ.get("SUB_FONT", "Arial Black")},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,6,3,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events, srt = [], []
    for idx, group in enumerate(_chunks(clip.words), 1):
        g_start = group[0]["start"] - clip.start
        g_end = group[-1]["end"] - clip.start
        srt.append(f"{idx}\n{_srt_time(g_start)} --> {_srt_time(g_end)}\n{' '.join(x['word'] for x in group)}\n")
        for k, word in enumerate(group):
            s = word["start"] - clip.start
            e = group[k + 1]["start"] - clip.start if k + 1 < len(group) else g_end
            parts = []
            for m, other in enumerate(group):
                txt = _clean(other["word"]).upper()
                parts.append(f"{{\\c{highlight}\\fscx110\\fscy110}}{txt}{{\\r}}" if m == k else txt)
            events.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Default,,0,0,0,,{' '.join(parts)}")
    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    srt_path.write_text("\n".join(srt), encoding="utf-8")


def render_clip(src: Path, clip: Clip, out_dir: Path, index: int, vertical: bool, style: dict) -> dict:
    name = f"clip_{index:02d}"
    ass, srt, mp4 = out_dir / f"{name}.ass", out_dir / f"{name}.srt", out_dir / f"{name}.mp4"
    write_subtitles(clip, ass, srt, vertical, style)
    if vertical:
        vf = "crop='min(iw,ih*9/16)':ih,scale=1080:1920,setsar=1"
    else:
        vf = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
    vf += f",subtitles={ass.name}"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{clip.start:.3f}", "-to", f"{clip.end:.3f}", "-i", str(src.resolve()),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        f"{name}.tmp.mp4",
    ]
    # cwd = carpeta de salida para evitar problemas de rutas de Windows en el filtro subtitles
    result = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg fallo: {result.stderr[-800:]}")
    # se renombra al final: si el servidor se apaga a medias no queda un clip roto
    os.replace(out_dir / f"{name}.tmp.mp4", mp4)
    return {
        "index": index,
        "title": clip.title,
        "reason": clip.reason,
        "score": clip.score,
        "start": round(clip.start, 2),
        "end": round(clip.end, 2),
        "duration": round(clip.end - clip.start, 1),
        "video": mp4.name,
        "srt": srt.name,
        "text": " ".join(w["word"] for w in clip.words),
    }
