# MiniMax-H3 serverless worker (ComfyUI + Turbo LoRA)
#
# CUDA 12.8 — минимум для Blackwell (5090). На более старом рантайме карта
# просто не заведётся, а ошибка будет невнятной ("no kernel image available").
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    COMFY_HOST=127.0.0.1:8188 \
    COMFY_INPUT_DIR=/comfyui/input \
    COMFY_OUTPUT_DIR=/comfyui/output

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip git ffmpeg curl aria2 \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python

# PyTorch под cu128. Ставим ПЕРЕД ComfyUI: иначе его requirements подтянут
# сборку под другую CUDA, и на 5090 всё развалится в рантайме.
# apt python3-pip = pip для СИСТЕМНОГО 3.10, а python=3.11 — поэтому только
# python -m pip: иначе пакеты не в том интерпретаторе (крашлуп 11.08).
RUN python -m pip install --upgrade pip && \
    python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128

# Пин на релиз, не master: v0.31.0 несёт фикс аудио-сэмплера H3 (kijai
# PR#15243, ModelSamplingAV) и лора-лоадер с автодетектом pruned-базы.
# Spectrum ниже требует ComfyUI >= e377e263 и покрывает контракт v0.31.
ARG COMFY_REF=v0.31.0
RUN git clone https://github.com/comfyanonymous/ComfyUI /comfyui && \
    cd /comfyui && git checkout ${COMFY_REF} && \
    python -m pip install -r requirements.txt

# ── Кастом-ноды ─────────────────────────────────────────────────────────
# Состав продиктован эталонным воркфлоу (workflows/fl_turbo_example.ui.json):
#   Turbo          — свой сэмплер, ведёт видео и аудио по РАЗНЫМ расписаниям;
#                    стоковый на 6-8 шагах перешагивает аудио, и звук рвётся;
#   KJNodes        — загрузчик модели + патч sage-attention (memory-efficient),
#                    без него 33B в 32 ГБ не укладывается;
#   Spectrum       — SpectrumApplyMiniMaxH3, качество на малых шагах;
#   SolAttn        — альтернативный attention-патч (triton);
#   VideoHelper    — VHS_VideoCombine, сборка mp4 из кадров + аудио.
RUN set -eux; \
    for repo in \
        "https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo" \
        "https://github.com/kijai/ComfyUI-KJNodes" \
        "https://github.com/kijai/ComfyUI-SolAttn_triton" \
        "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite" \
        "https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3" \
    ; do \
        name=$(basename "$repo"); \
        git clone --depth 1 "$repo" "/comfyui/custom_nodes/$name"; \
        if [ -f "/comfyui/custom_nodes/$name/requirements.txt" ]; then \
            python -m pip install -r "/comfyui/custom_nodes/$name/requirements.txt"; \
        fi; \
    done

# sageattention — рычаг скорости/памяти, который дёргают патч-ноды выше.
# Ставим отдельно: в requirements кастом-нод он не всегда прописан, а без
# пакета ноды молча деградируют к обычному attention.
RUN python -m pip install sageattention || echo "[warn] sageattention не встал — патчи будут работать вхолостую"

# Spectrum (xmarre) добавлен в список выше 2026-08-10 — репозиторий появился.
# ⚠️ По README Spectrum — аппроксиматор: на сложном движении возможны
# «malformed fingers/limbs». В прод-шаблонах включать осознанно, A/B на сиде.

WORKDIR /

COPY requirements.txt /requirements.txt
RUN python -m pip install -r /requirements.txt

COPY handler.py /handler.py
COPY scripts/ /scripts/
COPY workflows/ /workflows/
RUN chmod +x /scripts/*.sh

# Модели по умолчанию приезжают с network volume (/runpod-volume) — образ
# остаётся лёгким и итерации быстрые. BAKE_MODELS=1 запекает их внутрь:
# образ ~60 ГБ, зато под не зависит от тома и стартует быстрее.
ARG BAKE_MODELS=0
RUN if [ "$BAKE_MODELS" = "1" ]; then /scripts/download_models.sh /comfyui/models; fi

ENTRYPOINT ["/scripts/start.sh"]
