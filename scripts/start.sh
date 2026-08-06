#!/usr/bin/env bash
# Поднимаем ComfyUI, затем serverless-handler (или голый ComfyUI для отладки).
set -euo pipefail

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

COMFY_ARGS="--listen 0.0.0.0 --port 8188 --disable-auto-launch --disable-metadata"

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
