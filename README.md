# h3-worker — RunPod serverless воркер для MiniMax-H3 (i2v + звук)

Генерация видео из фото на MiniMax-H3 с Turbo-LoRA (6-8 шагов вместо ~20).
Совместим по контракту с текущим видео-воркером, поэтому `user-api` менять не
нужно: тот же вход `{"input": {"workflow": ..., "images": [...]}}`, тот же
выход `{"output": {"video": "<base64 mp4>"}}`.

## Почему отдельный репозиторий

Krea-образ (`soldatsc/newcrpho`) общий с prompt-lab ботом — правки там строго
аддитивные. H3 тянет другой стек (33B модель, свои VAE, кастом-ноды под Turbo),
мешать их в одном образе нельзя.

## Контракт

**Вход** (RunPod `/run`):
```json
{
  "input": {
    "workflow": { "...": "ComfyUI API-format JSON" },
    "images": [{ "name": "input.jpg", "image": "<base64>" }],
    "with_audio": true
  },
  "policy": { "executionTimeout": 600000 }
}
```
* `images` — кладутся в `ComfyUI/input/` под своими именами; workflow ссылается
  на них по `name`.
* `with_audio` (необязательный, по умолчанию `true`) — если `false`, звуковая
  дорожка не подмешивается в MP4. Сам сэмплинг всё равно совместный: в H3
  видео и аудио рождаются одним проходом, отдельного video-only режима нет.

**Выход**:
```json
{ "video": "<base64 mp4>", "seed": 12345, "frames": 149, "duration_s": 6.2 }
```
При ошибке — `{"error": "...", "stage": "..."}`; RunPod пометит джобу FAILED,
и наш сторож (`runpod_terminate`) отработает как для остальных пулов.

## Грабли, заложенные в конструкцию

🔴 **ComfyUI умирает после 283-500 задач.** Замер по проду 2026-08-06 на
krea-пуле: воркеры стабильно падают на нескольких сотнях задач с
`ComfyUI server (127.0.0.1:8188) not reachable` — утечка памяти. Под при этом
жив и продолжает брать задачи, штампуя мгновенные фейлы, пока сторож его не
уберёт. Поэтому здесь:
* `MAX_JOBS_BEFORE_EXIT` (по умолчанию 200) — воркер сам корректно выходит,
  RunPod поднимает свежий;
* health-check ComfyUI ПЕРЕД взятием задачи — если процесс мёртв, воркер
  выходит сразу, а не отдаёт юзеру ошибку.

## Модели

| Файл | Куда | Зачем |
|---|---|---|
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `models/diffusion_models/` | i2v (first/last frame) |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `models/text_encoders/` | текстовый энкодер |
| `minimax_h3_video_vae_fp16.safetensors` | `models/vae/` | видео |
| `minimax_h3_audio_vae_fp32.safetensors` | `models/vae/` | звук |
| `minimax_h3_turbo_4step_ckpt500_pruned_comfyui.safetensors` | `models/loras/` | Turbo (6-8 шагов) |

Turbo-LoRA берётся из `drbaph/MiniMax-H3-Turbo-Lora-ComfyUI` — это конверсия
под **pruned**-базу и штатный ComfyUI-лоадер (оригинал `larryvrh` рассчитан на
кастом-ноды). Вариант `ckpt500` без EMA: новее и резче, EMA гостит на движении.

**Два способа доставки моделей:**
1. **Network volume** (по умолчанию, быстрые итерации) — том монтируется в
   `/runpod-volume`, `scripts/download_models.sh` заполняет его один раз.
2. **Запекание в образ** — `--build-arg BAKE_MODELS=1`. Образ ~60 ГБ, зато нет
   зависимости от тома и холодный старт короче.

## Сборка и деплой

```bash
docker build -t <registry>/h3-worker:0.1 .
docker push <registry>/h3-worker:0.1
```
Дальше в RunPod: новый serverless endpoint на этот образ, network volume
примонтировать в `/runpod-volume`, GPU — 5090/4090 (32/24 ГБ).

⚠️ **FlashBoot держать выключенным** — он паузит под вместо остановки, процесс
живёт вечно и копит память (см. «Грабли» выше).

## Локальная проверка без RunPod

```bash
docker run --gpus all -p 8188:8188 -v /path/to/models:/runpod-volume \
  -e SERVE_API=1 <registry>/h3-worker:0.1
```
Откроется обычный ComfyUI на `localhost:8188` — тот же образ, что поедет в
serverless, поэтому воркфлоу отлаживается ровно в той среде, где потом побежит.
