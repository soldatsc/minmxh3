"""RunPod serverless handler для MiniMax-H3 (видео + звук).

    вход  {"input": {"workflow": {...}, "images": [{"name","image"}], "with_audio": bool}}
    выход {"video": "<base64 mp4>", "duration_s": ...}

Конструкция учитывает поведение долгоживущего ComfyUI-процесса:
* он упирается в память и перестаёт отвечать → выходим раньше по счётчику задач;
* мёртвый процесс = серия мгновенных ошибок → health-check ДО взятия задачи;
* входные изображения удаляем после задачи, чтобы тёплый под не переиспользовал
  чужой кадр.
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

# Сколько задач берём до добровольного выхода. 200 — с запасом до наблюдаемого
# порога отказа (~280+ задач). RunPod поднимет новый под.
MAX_JOBS_BEFORE_EXIT = int(os.environ.get("MAX_JOBS_BEFORE_EXIT", "200"))
# Сколько ждём ComfyUI при старте пода (модели 33B грузятся долго).
COMFY_BOOT_TIMEOUT_S = int(os.environ.get("COMFY_BOOT_TIMEOUT_S", "600"))
# Потолок на всю задачу, считая от её начала (проверка ComfyUI, постановка,
# генерация): держим ниже executionTimeout эндпоинта (600 с) — его превышение
# RunPod карает остановкой воркера, а не только провалом задачи. 570: раньше
# 540 с отсчитывались после постановки, теперь от начала — время на генерацию
# то же, 30 с запаса на историю, base64 и снятие задачи.
JOB_TIMEOUT_S = int(os.environ.get("JOB_TIMEOUT_S", "570"))
# Ожидание ответа ComfyUI на постановку задачи и на websocket (не дольше
# остатка JOB_TIMEOUT_S). Было 30 с: 20.10 на медленном сетевом диске свежий
# под отвечал на первую задачу дольше — хендлер бросал её (Failed за 30,5 с),
# а следующую считал «ComfyUI мёртв» и перезапускал под. Каждый новый под
# сжигал две задачи людей.
COMFY_HTTP_TIMEOUT_S = int(os.environ.get("COMFY_HTTP_TIMEOUT_S", "300"))
# Сколько перед задачей ждём ComfyUI, который не отвечает, но процесс жив (занят).
PRE_JOB_ALIVE_WAIT_S = int(os.environ.get("PRE_JOB_ALIVE_WAIT_S", "120"))

_jobs_done = 0

# ── RAM-проба (2026-08-16) ────────────────────────────────────────────────
# Воркеры умирают по RAM ПОСРЕДИ первой джобы на одном и том же шаге семплинга
# (лог 16.08: 6-7/8, хост 251 ГБ, лимит контейнера 83) — не накопление между
# джобами. Чтобы не гадать, что растёт, пишем раз в RAM_PROBE_INTERVAL_S:
# cgroup memory.current/max контейнера + RSS процесса ComfyUI. Кривая в логе
# RunPod → видно точку и скорость роста. RAM_PROBE_INTERVAL_S=0 — выключить.
import threading

RAM_PROBE_INTERVAL_S = float(os.environ.get("RAM_PROBE_INTERVAL_S", "5"))


def _read_first(paths: tuple[str, ...]) -> str | None:
    for p in paths:
        try:
            return Path(p).read_text().strip()
        except OSError:
            continue
    return None


def _cgroup_mem() -> tuple[float | None, float | None]:
    """(current_gb, max_gb) контейнера; None — не смогли прочитать."""
    cur = _read_first(("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"))
    mx = _read_first(("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"))
    def _gb(v: str | None) -> float | None:
        if v is None or v == "max":
            return None
        try:
            n = int(v)
        except ValueError:
            return None
        return None if n > 9_000_000_000_000 else round(n / 1024 ** 3, 1)
    return _gb(cur), _gb(mx)


def _comfy_pid() -> str | None:
    """PID процесса `python main.py` (ComfyUI) по /proc — без psutil.
    У умершего (зомби) процесса cmdline пустой — он не находится."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if b"main.py" in cmd and b"python" in cmd:
            return pid
    return None


def _comfy_rss_gb() -> float | None:
    """RSS процесса ComfyUI."""
    try:
        pid = _comfy_pid()
        if pid is None:
            return None
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024 ** 2, 1)
    except Exception:  # noqa: BLE001 — проба не должна ничего ронять
        pass
    return None


def _ram_probe(stop: threading.Event, tag: str) -> None:
    t0 = time.time()
    while not stop.wait(RAM_PROBE_INTERVAL_S):
        cur, mx = _cgroup_mem()
        rss = _comfy_rss_gb()
        print(
            f"[h3-worker] RAM t+{int(time.time() - t0)}s job={tag} "
            f"cgroup={cur}/{mx}GB comfy_rss={rss}GB",
            flush=True,
        )


def _start_ram_probe(tag: str) -> threading.Event:
    stop = threading.Event()
    if RAM_PROBE_INTERVAL_S > 0:
        threading.Thread(target=_ram_probe, args=(stop, tag), daemon=True).start()
    return stop


def _comfy_alive(timeout: float = 5) -> bool:
    """ComfyUI отвечает? Признак мёртвого пода: `not reachable after retries`."""
    try:
        r = requests.get(f"http://{COMFY_HOST}/system_stats", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _left_s(job_deadline: float) -> float:
    """Сколько секунд осталось у задачи; кончилось — TimeoutError."""
    left = job_deadline - time.time()
    if left <= 0:
        raise TimeoutError(f"job exceeded {JOB_TIMEOUT_S}s")
    return left


def _comfy_ready_for_job(job_deadline: float) -> bool:
    """ComfyUI отвечает — берём задачу. Не отвечает, но процесс жив — он занят,
    ждём до PRE_JOB_ALIVE_WAIT_S (и не дольше бюджета задачи). Процесса нет или
    не дождались — под мёртв. Раньше хватало одного запроса на 5 с, и занятый
    ComfyUI считался мёртвым (20.10)."""
    wait_until = min(time.time() + PRE_JOB_ALIVE_WAIT_S, job_deadline)
    while True:
        if _comfy_alive(timeout=10):
            return True
        if _comfy_pid() is None or time.time() >= wait_until:
            return False
        print("[h3-worker] ComfyUI busy, process alive — waiting", flush=True)
        time.sleep(5)


def _warm_up() -> None:
    """До первой задачи: /object_info заставляет ComfyUI один раз перечислить
    модели на сетевом диске — на свежем поде именно первая задача упиралась в
    медленный диск (20.10). Ошибка не фатальна: тогда прогреет сама задача."""
    t0 = time.time()
    try:
        requests.get(f"http://{COMFY_HOST}/object_info", timeout=COMFY_BOOT_TIMEOUT_S).raise_for_status()
        print(f"[h3-worker] warm-up done in {time.time() - t0:.0f}s", flush=True)
    except Exception as e:  # noqa: BLE001 — прогрев не должен ронять под
        print(f"[h3-worker] warm-up failed after {time.time() - t0:.0f}s: {e}", flush=True)


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


def _open_ws(client_id: str, job_deadline: float) -> "websocket.WebSocket":
    """Websocket открываем ДО постановки задачи — иначе быстрый финиш или ошибка
    задачи проскочат мимо, и хендлер прождёт весь бюджет (ревью 20.10)."""
    ws = websocket.WebSocket()
    ws.connect(f"ws://{COMFY_HOST}/ws?clientId={client_id}",
               timeout=min(COMFY_HTTP_TIMEOUT_S, _left_s(job_deadline)))
    return ws


def _queue_prompt(workflow: dict, client_id: str, prompt_id: str, job_deadline: float) -> str:
    # prompt_id свой (ComfyUI v0.31 принимает UUID клиента): по нему снимаем
    # задачу с ComfyUI, если сами её бросили.
    r = requests.post(
        f"http://{COMFY_HOST}/prompt",
        json={"prompt": workflow, "client_id": client_id, "prompt_id": prompt_id},
        timeout=min(COMFY_HTTP_TIMEOUT_S, _left_s(job_deadline)),
    )
    r.raise_for_status()
    return r.json()["prompt_id"]


def _drop_prompt(prompt_id: str) -> None:
    """Задачу бросили (таймаут/ошибка) — снимаем её с ComfyUI: из очереди и, если
    уже идёт, прерываем. Иначе он дорендерит её вхолостую, а следующая задача
    встанет за ней. Best effort: ComfyUI может не ответить."""
    for path, body in (("/queue", {"delete": [prompt_id]}), ("/interrupt", {"prompt_id": prompt_id})):
        try:
            requests.post(f"http://{COMFY_HOST}{path}", json=body, timeout=5)
        except Exception as e:  # noqa: BLE001
            print(f"[h3-worker] {path} for {prompt_id} failed: {e}", flush=True)


def _await_completion(ws: "websocket.WebSocket", prompt_id: str, job_deadline: float) -> dict:
    """Ждём выполнения через websocket, историю читаем по завершении.

    Websocket, а не поллинг /history: на длинных видео поллинг раз в секунду
    заваливает ComfyUI лишними запросами, а нам важна каждая секунда GPU.
    """
    while True:
        ws.settimeout(_left_s(job_deadline))
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

    started = time.time()
    job_deadline = started + JOB_TIMEOUT_S

    # Мёртвый ComfyUI — не берём задачу вообще: пусть RunPod отдаст её другому
    # поду, а этот уйдёт. Иначе получаем серию мгновенных фейлов у живых юзеров.
    if not _comfy_ready_for_job(job_deadline):
        _exit_worker("comfyui dead before job")
        return {"error": "ComfyUI is not reachable", "stage": "healthcheck"}

    client_id = str(uuid.uuid4())
    prompt_id = str(uuid.uuid4())
    queued = finished = False
    staged: list[Path] = []
    _probe_stop = _start_ram_probe(str(job.get("id", "?"))[:12])
    _c0, _m0 = _cgroup_mem()
    print(f"[h3-worker] RAM at job start: cgroup={_c0}/{_m0}GB comfy_rss={_comfy_rss_gb()}GB", flush=True)
    try:
        staged = _stage_images(job_input.get("images") or [])
        ws = _open_ws(client_id, job_deadline)
        try:
            queued = True  # с этого момента задача может оказаться в очереди ComfyUI
            prompt_id = _queue_prompt(workflow, client_id, prompt_id, job_deadline)
            history = _await_completion(ws, prompt_id, job_deadline)
            finished = True
        finally:
            try:
                ws.close()
            except Exception:
                pass

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
    except Exception as e:  # noqa: BLE001 — текст наружу, RunPod пометит FAILED
        return {"error": f"{type(e).__name__}: {str(e)[:300]}", "stage": "generate"}
    finally:
        if queued and not finished:
            _drop_prompt(prompt_id)
        _probe_stop.set()
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
    _warm_up()
    print("[h3-worker] ComfyUI is up, serving", flush=True)
    runpod.serverless.start({"handler": handler})
