#!/usr/bin/env bash
# Заливка моделей MiniMax-H3 (+ Turbo LoRA) в каталог ComfyUI.
#
# Использование:
#   ./download_models.sh /runpod-volume/models     # на network volume (обычный путь)
#   ./download_models.sh /comfyui/models           # внутрь образа (BAKE_MODELS=1)
#
# Приватных репозиториев тут нет, токен не нужен. Если HF начнёт лимитировать —
# положить токен в HF_TOKEN, aria2 подхватит через заголовок.
set -euo pipefail

DEST="${1:-/comfyui/models}"
BASE="https://huggingface.co"

dl() {  # dl <repo> <файл> <подпапка>
    local repo="$1" file="$2" sub="$3"
    local dir="$DEST/$sub"
    mkdir -p "$dir"
    if [ -f "$dir/$file" ]; then
        echo "[models] $file уже на месте — пропускаем"
        return 0
    fi
    echo "[models] качаем $file → $sub"
    aria2c -x 8 -s 8 -k 1M --console-log-level=warn \
        ${HF_TOKEN:+--header="Authorization: Bearer $HF_TOKEN"} \
        -d "$dir" -o "$file" \
        "$BASE/$repo/resolve/main/$file"
}

# ── База: i2v (first/last frame) в pruned_int8 — влезает в 24-32 ГБ ──
dl "Comfy-Org/MiniMax-H3" "split_files/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors" "diffusion_models"

# ── Текстовый энкодер ──
dl "Comfy-Org/MiniMax-H3" "split_files/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors" "text_encoders"

# ── VAE: видео + аудио (аудио нужен даже для «без звука» — модель считает обе ветки) ──
dl "Comfy-Org/MiniMax-H3" "split_files/vae/minimax_h3_video_vae_fp16.safetensors" "vae"
dl "Comfy-Org/MiniMax-H3" "split_files/vae/minimax_h3_audio_vae_fp32.safetensors" "vae"

# ── Turbo-LoRA: 6-8 шагов вместо ~20 ──
# Именно pruned-конверсия под штатный лоадер (оригинал larryvrh — под кастом-ноды).
# ckpt500 без EMA: новее и резче; EMA-вариант мылит и гостит на движении.
dl "drbaph/MiniMax-H3-Turbo-Lora-ComfyUI" "minimax_h3_turbo_4step_ckpt500_pruned_comfyui.safetensors" "loras"

echo "[models] готово. Содержимое $DEST:"
find "$DEST" -maxdepth 2 -type f -name "*.safetensors" -exec ls -lh {} \; | awk '{print $5, $9}'
