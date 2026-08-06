"""RunPod serverless handler для MiniMax-H3 (видео + звук).

Контракт совместим с текущим видео-воркером — user-api не меняется:
    вход  {"input": {"workflow": {...}, "images": [{"name","image"}], "with_audio": bool}}
    выход {"video": "<base64 mp4>", "seed": ..., "frames": ..., "duration_s": ...}

Ключевые решения продиктованы проданными граблями (см. README):
* ComfyUI течёт по памяти и умирает на 283-500 задаче → сами выходим раньше;
* мёртвый ComfyUI = мгновенные фейлы у живых юзеров → health-check ДО задачи;
* картинки юзера кладём во временные имена и удаляем после, чтобы тёплый
  воркер не переиспользовал чужой кадр (та же защита, что в krea-воркере).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import requests
import runpod
import websocket

COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1:8188")
COMFY_INPUT_DIR = Path(os.environ.get("COMFY_INPUT_DIR", "/comfyui/input"))
COMFY_OUTPUT_DIR = Path(os.environ.get("COMFY_OUTPUT_DIR", "/comfyui/output"))

# Сколько задач берём до добровольного выхода. 200 — с запасом до минимальной
# наблюдаемой смерти (283 задачи на krea-пуле 06.08). RunPod поднимет новый под.
MAX_JOBS_BEFORE_EXIT = int(os.environ.get("MAX_JOBS_BEFORE_EXIT", "200"))
# Сколько ждём ComfyUI при старте пода (модели 33B грузятся долго).
COMFY_BOOT_TIMEOUT_S = int(os.environ.get("COMFY_BOOT_TIMEOUT_S", "600"))
# Потолок на одну генерацию. Наш поллер в user-api ждёт до 600с — держим ниже,
# чтобы воркер сам вернул ошибку раньше, чем клиент отвалится по таймауту.
JOB_TIMEOUT_S = int(os.environ.get("JOB_TIMEOUT_S", "540"))

_jobs_done = 0


def _comfy_alive() -> bool:
    """ComfyUI отвечает? Это тот самый признак, по которому в проде ловили
    мёртвые поды (`not reachable after multiple retries`)."""
    try:
        r = requests.get(f"http://{COMFY_HOST}/system_stats", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def _wait_for_comfy(timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _comfy_alive():
            return True
        time.sleep(2)
    return False


def _stage_images(images: list[dict]) -> list[Path]:
    """Кладём присланные картинки в input ComfyUI. Возвращаем пути для уборки."""
    staged: list[Path] = []
    COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    for item in images or []:
        name = item.get("name")
        data = item.get("image")
        if not name or not data:
            continue
        # Никаких путей от клиента — только имя файла.
        safe = Path(name).name
        dst = COMFY_INPUT_DIR / safe
        dst.write_bytes(base64.b64decode(data))
        staged.append(dst)
    return staged


def _queue_prompt(workflow: dict, client_id: str) -> str:
    r = requests.post(
        f"http://{COMFY_HOST}/prompt",
        json={"prompt": workflow, "client_id": client_id},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["prompt_id"]


def _await_completion(prompt_id: str, client_id: str, timeout_s: int) -> dict:
    """Ждём выполнения через websocket, историю читаем по завершении.

    Websocket, а не поллинг /history: на длинных видео поллинг раз в секунду
    заваливает ComfyUI лишними запросами, а нам важна каждая секунда GPU.
    """
    ws = websocket.WebSocket()
    ws.connect(f"ws://{COMFY_HOST}/ws?clientId={client_id}", timeout=30)
    ws.settimeout(timeout_s)
    deadline = time.time() + timeout_s
    try:
        while time.time() < deadline:
            msg = ws.recv()
            if isinstance(msg, bytes):
                continue  # превью-кадры — игнорируем
            data = json.loads(msg)
            if data.get("type") == "executing":
                d = data.get("data", {})
                if d.get("node") is None and d.get("prompt_id") == prompt_id:
                    break
            elif data.get("type") == "execution_error":
                d = data.get("data", {})
                if d.get("prompt_id") == prompt_id:
                    raise RuntimeError(
                        f"ComfyUI execution error: {str(d.get('exception_message'))[:300]}"
                    )
        else:
            raise TimeoutError(f"generation exceeded {timeout_s}s")
    finally:
        try:
            ws.close()
        except Exception:
            pass

    r = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    r.raise_for_status()
    return r.json().get(prompt_id, {})


def _collect_video(history: dict) -> Path | None:
    """Ищем итоговый файл среди выходов. H3 отдаёт mp4 (видео+звук в контейнере)."""
    candidates: list[Path] = []
    for node_out in (history.get("outputs") or {}).values():
        for key in ("gifs", "videos", "audio", "files", "images"):
            for item in node_out.get(key, []) or []:
                fn = item.get("filename")
                if not fn:
                    continue
                sub = item.get("subfolder") or ""
                p = COMFY_OUTPUT_DIR / sub / fn
                if p.suffix.lower() in (".mp4", ".webm", ".mkv") and p.exists():
                    candidates.append(p)
    if not candidates:
        return None
    # Самый свежий — на случай, если воркфлоу писал промежуточные файлы.
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _strip_audio(src: Path) -> Path:
    """Отдельный mp4 без звуковой дорожки (режим with_audio=false).

    Пересжатия нет — только выбрасываем дорожку, поэтому это доли секунды.
    """
    dst = src.with_name(f"{src.stem}_noaudio{src.suffix}")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-an", "-c:v", "copy", str(dst)],
        check=True, capture_output=True, timeout=120,
    )
    return dst


def handler(job: dict) -> dict:
    global _jobs_done
    job_input = job.get("input") or {}
    workflow = job_input.get("workflow")
    if not isinstance(workflow, dict) or not workflow:
        return {"error": "input.workflow is required (ComfyUI API-format JSON)",
                "stage": "validate"}

    # Мёртвый ComfyUI — не берём задачу вообще: пусть RunPod отдаст её другому
    # поду, а этот уйдёт. Иначе получаем серию мгновенных фейлов у живых юзеров.
    if not _comfy_alive():
        _exit_worker("comfyui dead before job")
        return {"error": "ComfyUI is not reachable", "stage": "healthcheck"}

    client_id = str(uuid.uuid4())
    staged: list[Path] = []
    started = time.time()
    try:
        staged = _stage_images(job_input.get("images") or [])
        prompt_id = _queue_prompt(workflow, client_id)
        history = _await_completion(prompt_id, client_id, JOB_TIMEOUT_S)

        video_path = _collect_video(history)
        if video_path is None:
            return {"error": "no video in ComfyUI output", "stage": "collect"}

        if job_input.get("with_audio", True) is False:
            video_path = _strip_audio(video_path)

        payload = base64.b64encode(video_path.read_bytes()).decode("ascii")
        result = {
            "video": payload,
            "duration_s": round(time.time() - started, 1),
        }
        if job_input.get("seed") is not None:
            result["seed"] = job_input["seed"]
        return result
    except TimeoutError as e:
        return {"error": str(e), "stage": "generate"}
    except Exception as e:  # noqa: BLE001 — наружу отдаём текст, RunPod пометит FAILED
        return {"error": f"{type(e).__name__}: {str(e)[:300]}", "stage": "generate"}
    finally:
        for p in staged:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        # Выходим ДО того, как течь дойдёт до критической массы.
        _jobs_done += 1
        if _jobs_done >= MAX_JOBS_BEFORE_EXIT:
            _exit_worker(f"job budget reached ({_jobs_done})")


def _exit_worker(reason: str) -> None:
    """Корректный выход: RunPod поднимет свежий под с чистой памятью."""
    print(f"[h3-worker] exiting: {reason}", flush=True)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    if not _wait_for_comfy(COMFY_BOOT_TIMEOUT_S):
        print("[h3-worker] ComfyUI did not come up in time", flush=True)
        sys.exit(1)
    print("[h3-worker] ComfyUI is up, serving", flush=True)
    runpod.serverless.start({"handler": handler})
