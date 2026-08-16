#!/usr/bin/env bash
# Поднимаем ComfyUI, затем serverless-handler (или голый ComfyUI для отладки).
set -euo pipefail

# ── Самозащита от маленьких хостов (2026-08-16) ────────────────────────────
# RunPod раздаёт воркерам RAM по хосту (46 / 60 / 94+ ГБ), напрямую не
# ограничить. H3 держит в RAM ~40 ГБ моделей (fl2va 20 + qwen3vl 15 + VAE 5)
# — на 46 ГБ воркер гарантированно ловит OOM ПОСРЕДИ джобы (юзер ждёт
# 4 мин и получает ошибку). Лучше выйти сразу: RunPod поднимет другой воркер,
# как правило на другом хосте. Порог — env MIN_RAM_GB (0 = выключить).
MIN_RAM_GB="${MIN_RAM_GB:-56}"
if [ "$MIN_RAM_GB" != "0" ]; then
    _lim=""
    if [ -r /sys/fs/cgroup/memory.max ]; then
        _lim="$(cat /sys/fs/cgroup/memory.max)"                       # cgroup v2
    elif [ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]; then
        _lim="$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes)"     # cgroup v1
    fi
    if [ -n "$_lim" ] && [ "$_lim" != "max" ] && [ "$_lim" -lt 9000000000000 ] 2>/dev/null; then
        _gb=$(( _lim / 1024 / 1024 / 1024 ))
        echo "[start] container RAM limit: ${_gb} GB (min ${MIN_RAM_GB})"
        if [ "$_gb" -lt "$MIN_RAM_GB" ]; then
            echo "[start] UNDERSIZED HOST: ${_gb} GB < ${MIN_RAM_GB} GB — exiting so RunPod reschedules elsewhere"
            sleep 20   # не крутить рестарт-цикл впритык, дать шедулеру время
            exit 1
        fi
    else
        echo "[start] container RAM limit unknown (${_lim:-n/a}) — guard skipped"
    fi
fi

MODELS_SRC="${MODELS_DIR:-/runpod-volume/models}"

# Модели с network volume подключаем симлинками по подпапкам, а не одной
# заменой каталога: кастом-ноды доустанавливают свои веса в models/, и
# перетирание всего каталога их бы снесло.
if [ -d "$MODELS_SRC" ]; then
    echo "[start] linking models from $MODELS_SRC"
    for sub in diffusion_models text_encoders vae loras clip clip_vision upscale_models; do
        if [ -d "$MODELS_SRC/$sub" ]; then
            mkdir -p "/comfyui/models/$sub"
            # -n: не трогаем то, что уже запечено в образ
            ln -sfn "$MODELS_SRC/$sub"/* "/comfyui/models/$sub/" 2>/dev/null || true
        fi
    done
else
    echo "[start] no network volume at $MODELS_SRC — рассчитываем на модели в образе"
fi

# --cache-classic (2026-08-16): дефолтный RAM_PRESSURE-кэш ComfyUI ≥0.30 держит
# выгруженные модели в RAM «до 100% системной RAM», а RAM меряет psutil-ом =
# по ХОСТУ RunPod, не по cgroup-лимиту контейнера → никогда не чистит → OOM
# (тот же корень, что у Krea-воркеров 16.08). classic чистится после каждой
# джобы до текущего графа, loader-ноды переиспользуются — скорость та же.
COMFY_ARGS="--listen 0.0.0.0 --port 8188 --disable-auto-launch --disable-metadata --cache-classic"

# 33B-модель не влезает в 24-32 ГБ целиком: отдаём адаптивную загрузку весов.
# Флаг подтверждать на первом прогоне — если хватает памяти, убрать (быстрее).
if [ "${LOWVRAM:-1}" = "1" ]; then
    COMFY_ARGS="$COMFY_ARGS --lowvram"
fi
if [ -n "${EXTRA_COMFY_ARGS:-}" ]; then
    COMFY_ARGS="$COMFY_ARGS $EXTRA_COMFY_ARGS"
fi

echo "[start] launching ComfyUI: $COMFY_ARGS"
cd /comfyui
python main.py $COMFY_ARGS &
COMFY_PID=$!

# SERVE_API=1 — режим локальной отладки: только ComfyUI, без RunPod-хендлера.
# Тот же образ, что уедет в прод, поэтому воркфлоу отлаживается в боевой среде.
if [ "${SERVE_API:-0}" = "1" ]; then
    echo "[start] debug mode: ComfyUI only, no serverless handler"
    wait $COMFY_PID
    exit $?
fi

python -u /handler.py
# Хендлер вышел (исчерпан бюджет задач или мёртвый ComfyUI) — гасим всё,
# RunPod поднимет свежий под с чистой памятью.
kill $COMFY_PID 2>/dev/null || true
wait $COMFY_PID 2>/dev/null || true
