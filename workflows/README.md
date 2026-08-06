# Воркфлоу

Сюда кладём ComfyUI-воркфлоу **в API-формате** (не в UI-формате!).
Экспорт: в ComfyUI меню → `Workflow → Export (API)`. UI-формат воркер не
примет — у него другая структура, ноды лежат в `nodes[]`, а не по id.

## Что уже лежит

**`fl_turbo_example.ui.json`** — эталонный воркфлоу автора Turbo-LoRA
(`drbaph/MiniMax-H3-Turbo-Lora-ComfyUI`), **UI-формат**. Внутри ровно то, что
нам нужно: `MiniMaxH3ImageToVideo` (i2v по первому/последнему кадру),
`LoraLoaderModelOnly` с Turbo, `MiniMaxH3SigmaShift`, раздельный декод видео и
аудио (`VAEDecode` + `VAEDecodeAudio`), сборка mp4 через `VHS_VideoCombine` и
патчи экономии памяти (sage-attention, spectrum).

Кастом-ноды под него уже стоят в образе — KJNodes, SolAttn, VideoHelperSuite,
Turbo. Кроме `SpectrumApplyMiniMaxH3`: публичного репозитория не нашлось, при
первом открытии ComfyUI отметит её как missing — либо удалить из графа, либо
доставить через Manager (и тогда дописать в Dockerfile).

## Что нужно сделать

Воркер принимает **только API-формат**, поэтому:

1. Поднять образ в режиме отладки:
   `docker run --gpus all -p 8188:8188 -v <модели>:/runpod-volume -e SERVE_API=1 <образ>`
2. Открыть `fl_turbo_example.ui.json` в ComfyUI, прогнать один клип, покрутить
   параметры под наше железо.
3. `Workflow → Export (API)` → сохранить сюда как `i2v_turbo.json`
   (и, если нужен отдельный вариант без звука, `i2v_turbo_noaudio.json`).

## Стартовые значения

Шаги 6-8 (на 4 мыло — автор LoRA сам это пишет), sampler `res_multistep`,
scheduler `simple`, video sigma shift 12, audio sigma shift 4-6.
Разрешение ~1 Мп, для 16:9 это 1344×768, стороны кратны 32.
Кадры прыгают по сетке 17k+5 при 24 fps (проверено 124-362).

## Про подстановку промпта

Воркер выполняет присланный JSON как есть. Значения (промпт, seed, имя входной
картинки) подставляет вызывающая сторона перед отправкой: находит нужную ноду по
id и правит её `inputs`. Поэтому **id нод в экспортируемом файле менять нельзя**
без синхронной правки билдера на стороне клиента.
