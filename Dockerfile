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
    && ln -sf /usr/bin/python3.11 /usr/bin/python

# PyTorch под cu128. Ставим ПЕРЕД ComfyUI: иначе его requirements подтянут
# сборку под другую CUDA, и на 5090 всё развалится в рантайме.
RUN pip install --upgrade pip && \
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128

ARG COMFY_REF=master
RUN git clone https://github.com/comfyanonymous/ComfyUI /comfyui && \
    cd /comfyui && git checkout ${COMFY_REF} && \
    pip install -r requirements.txt

# Кастом-ноды Turbo: нужны для звукового режима — их сэмплер ведёт видео и
# аудио по разным расписаниям. Стоковый на 6-8 шагах перешагивает аудио, и
# звук выходит рваным.
RUN git clone https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo \
        /comfyui/custom_nodes/ComfyUI-MiniMax-H3-Turbo && \
    if [ -f /comfyui/custom_nodes/ComfyUI-MiniMax-H3-Turbo/requirements.txt ]; then \
        pip install -r /comfyui/custom_nodes/ComfyUI-MiniMax-H3-Turbo/requirements.txt; \
    fi

WORKDIR /

COPY requirements.txt /requirements.txt
RUN pip install -r /requirements.txt

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
