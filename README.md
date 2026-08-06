# minmxh3 — RunPod serverless воркер для MiniMax-H3 (i2v + звук)

Генерация видео из изображения на MiniMax-H3 с Turbo-LoRA: 6-8 шагов вместо
~20. ComfyUI внутри, наружу — обычный serverless-хендлер.

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
  дорожка не подмешивается в MP4. Сам сэмплинг всё равно совместный: в H3 видео
  и аудио рождаются одним проходом, отдельного video-only режима нет.

**Выход**: `{ "video": "<base64 mp4>", "duration_s": 6.2 }`
При ошибке — `{"error": "...", "stage": "..."}`, джоба помечается FAILED.

## Устойчивость

Долгоживущий ComfyUI-процесс со временем упирается в память и перестаёт
отвечать (`127.0.0.1:8188 not reachable`), причём сам под остаётся жив и
продолжает брать задачи, штампуя мгновенные ошибки. Поэтому здесь:

* `MAX_JOBS_BEFORE_EXIT` (по умолчанию 200) — воркер корректно завершается сам,
  RunPod поднимает свежий под с чистой памятью;
* health-check ComfyUI **перед** взятием задачи — мёртвый под не берёт работу;
* входные изображения удаляются после задачи, чтобы тёплый под не переиспользовал
  чужой кадр.

⚠️ **FlashBoot держать выключенным**: он паузит под вместо остановки, процесс
живёт неограниченно долго и копит память.

## Модели

| Файл | Куда | Зачем |
|---|---|---|
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `models/diffusion_models/` | i2v (first/last frame) |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `models/text_encoders/` | текстовый энкодер |
| `minimax_h3_video_vae_fp16.safetensors` | `models/vae/` | видео |
| `minimax_h3_audio_vae_fp32.safetensors` | `models/vae/` | звук |
| `minimax_h3_turbo_4step_ckpt500_pruned_comfyui.safetensors` | `models/loras/` | Turbo (6-8 шагов) |

Turbo-LoRA берётся из `drbaph/MiniMax-H3-Turbo-Lora-ComfyUI` — это конверсия под
**pruned**-базу и штатный ComfyUI-лоадер (оригинал `larryvrh` рассчитан на
кастом-ноды). Вариант `ckpt500` без EMA: новее и резче, EMA гостит на движении.

Доставка моделей — network volume в `/runpod-volume` (по умолчанию, быстрые
итерации) либо запекание в образ: `--build-arg BAKE_MODELS=1` (образ ~60 ГБ, зато
короче холодный старт).

## Сборка

```bash
docker build -t <registry>/minmxh3:0.1 .
docker push <registry>/minmxh3:0.1
```

GPU: 32 ГБ (5090) или 24 ГБ (4090) — база 33B в pruned_int8. При нехватке памяти
поможет `LOWVRAM=1` (включён по умолчанию) и GGUF-варианты базы поменьше.

## Локальная отладка воркфлоу

```bash
docker run --gpus all -p 8188:8188 -v /path/to/models:/runpod-volume \
  -e SERVE_API=1 <registry>/minmxh3:0.1
```
Поднимется обычный ComfyUI на `localhost:8188` — тот же образ, что уходит в
serverless, поэтому воркфлоу отлаживается ровно в целевой среде.

## Настройки генерации

Шаги 6-8 (на 4 заметно мылит — ранний чекпойнт LoRA), sampler `res_multistep`,
scheduler `simple`, video sigma shift 12, audio sigma shift 4-6, сила LoRA
0.8-1.8. Разрешение ~1 Мп (16:9 → 1344×768), стороны кратны 32; число кадров
снапится к сетке 17k+5 при 24 fps.
