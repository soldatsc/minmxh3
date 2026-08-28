#!/usr/bin/env bash
# Заливка моделей MiniMax-H3 (+ Turbo LoRA) в каталог ComfyUI.
#
# Использование:
#   ./download_models.sh /runpod-volume/models     # на network volume (обычный путь)
#   ./download_models.sh /comfyui/models           # внутрь образа (BAKE_MODELS=1)
#
# Приватных репозиториев тут нет, токен не нужен. Если HF начнёт лимитировать —
# положить токен в HF_TOKEN, aria2 подхватит через заголовок.
#
# Итого ~40 ГБ: база 19.5 + энкодер 14.6 + VAE 5.5 + LoRA 0.6. Тому хватит 60 ГБ.
set -euo pipefail

DEST="${1:-/comfyui/models}"
BASE="https://huggingface.co"

dl() {  # dl <repo> <файл> <подпапка>
    local repo="$1" file="$2" sub="$3"
    local dir="$DEST/$sub"
    # 🔴 Только ИМЯ файла: в репозитории HF путь содержит подпапку
    # (vae/файл.safetensors), и если отдать её в -o, aria2 воспроизведёт её
    # внутри целевого каталога → models/vae/vae/файл. ComfyUI такие модели
    # не находит, а выглядит это как «скачалось, но модели нет».
    local name="${file##*/}"
    mkdir -p "$dir"
    if [ -f "$dir/$name" ]; then
        echo "[models] $name уже на месте — пропускаем"
        return 0
    fi
    echo "[models] качаем $name → $sub"
    aria2c -x 8 -s 8 -k 1M --console-log-level=warn \
        ${HF_TOKEN:+--header="Authorization: Bearer $HF_TOKEN"} \
        -d "$dir" -o "$name" \
        "$BASE/$repo/resolve/main/$file"
}

dl_named() {  # dl_named <repo> <URL-кодированный путь на HF> <подпапка> <имя-на-диске>
    local repo="$1" file="$2" sub="$3" name="$4"
    local dir="$DEST/$sub"
    mkdir -p "$dir"
    if [ -f "$dir/$name" ]; then
        echo "[models] $name уже на месте — пропускаем"
        return 0
    fi
    # $file — УЖЕ URL-кодированный путь: пробелы и запятые в именах HF легальны,
    # но aria2 их не экранирует сам, а звать python3 на этапе сборки нельзя
    # (venv может перехватить имя).
    echo "[models] качаем $name → $sub"
    aria2c -x 8 -s 8 -k 1M --console-log-level=warn         ${HF_TOKEN:+--header="Authorization: Bearer $HF_TOKEN"}         -d "$dir" -o "$name"         "$BASE/$repo/resolve/main/$file"
}

# ── Большие веса (~40 ГБ): база + энкодер + VAE ──────────────────────────
# ONLY_LORAS=1 их пропускает: раннер GitHub даёт ~14 ГБ, а слой torch+ComfyUI
# уже около того — запечь 40 ГБ в образ нельзя, они приезжают с тома.
if [ "${ONLY_LORAS:-0}" != "1" ]; then

# ── База: i2v (first/last frame) в pruned_int8 — влезает в 24-32 ГБ ──
dl "Comfy-Org/MiniMax-H3" "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors" "diffusion_models"   # 19.5 ГБ

# ── Текстовый энкодер ──
dl "Comfy-Org/MiniMax-H3" "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors" "text_encoders"   # 14.6 ГБ

# ── VAE: видео + аудио (аудио нужен даже для «без звука» — модель считает обе ветки) ──
dl "Comfy-Org/MiniMax-H3" "vae/minimax_h3_video_vae_fp16.safetensors" "vae"   # 4.9 ГБ
dl "Comfy-Org/MiniMax-H3" "vae/minimax_h3_audio_vae_fp32.safetensors" "vae"   # 0.6 ГБ

# ── Turbo-LoRA: 6-8 шагов вместо ~20 ──
# Именно pruned-конверсия под штатный лоадер (оригинал larryvrh — под кастом-ноды).
# ckpt500 без EMA: новее и резче; EMA-вариант мылит и гостит на движении.
dl "drbaph/MiniMax-H3-Turbo-Lora-ComfyUI" "minimax_h3_turbo_4step_ckpt500_pruned_comfyui.safetensors" "loras"

fi   # ONLY_LORAS

# ── NSFW-лоры (2026-08-28) ─────────────────────────────────────────────────
# Раньше приезжали на том руками, мимо сборки: если том потеряется, прод
# останется без генитальных лор и никто не поймёт почему. Теперь в скрипте.
# Силы держит user-api в web_settings.h3_settings, здесь только файлы.
# EXTRA_LORAS=0 — не качать (например, для лёгкого локального прогона).
if [ "${EXTRA_LORAS:-1}" != "0" ]; then
    HM="Hearmeman/minimax-h3-loras"
    dl "$HM" "HMNSFW-AIO-V2.5.safetensors"            "loras"   # акт целиком, 0.5-0.9
    dl "$HM" "Vagina_minimax-h3_epoch20.safetensors"  "loras"   # структура ж, 1.0
    # 597 МБ и формат LoKr — наш загрузчик может её не съесть, а в образ она
    # не влезает вместе с torch. По умолчанию НЕ качаем: сперва проверяем
    # тройку, потом решаем (WITH_BIG_LORAS=1 — положить на том).
    if [ "${WITH_BIG_LORAS:-0}" = "1" ]; then
        dl "$HM" "hmpussy_v6_epoch30.safetensors"     "loras"   # держит ж в движении, 0.35
    fi
    # 🔴 HMPenis: у автора на HF лежит УСТАРЕВШАЯ epoch35 (296 МБ, rank 32).
    # Актуальная epoch60 (74 МБ, rank 8, авторская сила 0.9) — только в зеркале,
    # и в имени файла пробелы и запятые, поэтому качаем отдельной строкой.
    dl_named "EllaPriest45/MinimaxH3_Actions"         "HMPenis%20v2.0%20-%20MinimaxH3%20-%20penis%2C0.9str.safetensors"         "loras" "HMPenisV2_e60.safetensors"
fi

echo "[models] готово. Содержимое $DEST:"
find "$DEST" -maxdepth 2 -type f -name "*.safetensors" -exec ls -lh {} \; | awk '{print $5, $9}'
