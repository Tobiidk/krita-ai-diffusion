# Krita AI Diffusion - UI Tweaks Fork

This fork is a working branch of
[Acly/krita-ai-diffusion](https://github.com/Acly/krita-ai-diffusion) focused on
Flux 2 / Klein workflows, faster sidebar control, clearer history metadata, and
more practical LoRA handling inside Krita.

The local checkout is intended to be symlinked into Krita's `pykrita` plugin
folder, so changes in this repo can be tested directly in the installed plugin
after restarting Krita.

## Current Branch

- Remote: `origin`
- Branch: `ui-tweaks`
- Baseline project: Krita plugin using ComfyUI as the backend.
- Main test target: Flux 2 Klein editing/generation workflows.

## Major Fork Changes

### Flux 2 / Klein Workflow

- Added Flux 2 focused style presets, including Klein variants.
- Added Flux 2 sampler presets, including RES4LYF sampler options:
  - `RES4LYF Flux 2 - RES 2M`
  - `RES4LYF Flux 2 - RES 3M`
  - `RES4LYF Flux 2 - RES 2S`
  - `RES4LYF Flux 2 - RES 3S`
  - `RES4LYF Flux 2 - DEIS 2M`
- Patched the workflow path so Flux 2 uses the correct custom sampler flow.
- Flux 2 CFG/guidance is intentionally disabled in the normal UI because the
  old slider path did not produce real output changes.
- Negative prompt remains disabled for Flux 2 in the current default path,
  because the current Flux 2 BasicGuider flow does not use CFG-style negative
  conditioning meaningfully.
- Added an optional Flux 2 Scheduled CFGGuider path via ComfyUI Inspire Pack.

### Guidance, Steps, and Denoise

- Added sidebar sampler preset, steps, and guidance controls for the active
  quality/live context.
- Sidebar and style-settings sampler switches preserve the current step count
  instead of reloading the preset default every time.
- Kept regular Flux 2 guidance hidden, but Scheduled CFG presets expose the
  guidance slider as the target `to_cfg` value.
- Reworked denoise handling so edit denoise is not just a misleading bundled
  steps display.
- History metadata now records denoise, actual steps, and total steps when
  applicable.

### Sidebar LoRA Controls

- Added compact LoRA quick toggles in the generation sidebar.
- LoRA entries can be enabled/disabled directly from the sidebar.
- LoRA strength can be adjusted directly from the sidebar.
- Added style-local LoRA presets in the sidebar:
  - Add a new preset from the dropdown.
  - Update the selected preset with the save button when its LoRA settings have
    unsaved changes.
  - Dirty presets show a trailing `*` in the dropdown.
  - Presets store the current LoRA order, enabled states, strengths, display
    names, and descriptions.
  - Reapply a saved LoRA preset from a dropdown.
  - Delete saved LoRA presets.
  - Presets are stored inside the active style JSON under `lora_presets`, not
    globally across all styles.
- Sidebar LoRA area uses a compact two-column layout.
- Sidebar LoRA area has a draggable height handle and remembers the chosen
  visible row count.
- LoRA sidebar labels support per-style custom display names.
- LoRA sidebar hover text supports per-style descriptions and wraps long text.
- Sidebar LoRA write-back preserves custom metadata fields.

### Style Settings LoRA Editing

- Added drag-to-reorder for LoRAs in style settings.
- Added per-LoRA `display_name` and `description` fields in the expanded LoRA
  settings panel.
- Added no-wheel combo boxes in settings so scrolling the page while hovering a
  dropdown does not accidentally change LoRA, style, sampler, or performance
  selections.
- Existing LoRA trigger words and default strength behavior are preserved.

Example LoRA style entry:

```json
{
    "name": "FemaleTongueMouthTeeth-Klein.safetensors",
    "strength": 1.5,
    "enabled": true,
    "display_name": "Mouth / Teeth Detail",
    "description": "Suggested strength: 1.0-1.5. Helps with open mouth, tongue, teeth, lips, and related facial detail."
}
```

Example style-local LoRA preset entry:

```json
{
    "lora_presets": {
        "Baseline Realism": [
            {
                "name": "AnimeToReal-Klein.safetensors",
                "strength": 0.7,
                "enabled": true,
                "display_name": "Anime to Real",
                "description": "Primary domain-shift LoRA."
            },
            {
                "name": "UltraReal-Klein-v4.safetensors",
                "strength": 0.5,
                "enabled": true
            }
        ]
    }
}
```

### Quick Generation Styles

- Added quick generation style buttons in the sidebar.
- Quick style generation keeps the selected quick style's model/checkpoint/text
  encoders but inherits current LoRAs and sampling settings from the active
  style.
- Quick style buttons now use a horizontal scroll strip instead of expanding or
  squeezing the sidebar.
- Quick style add/remove saves immediately.

### NAG Negative Guidance

- Added optional style-local NAG negative guidance settings for Flux-style
  `SamplerCustomAdvanced` workflows:
  - Enable/disable NAG per style preset.
  - Tune `nag_scale`, `nag_tau`, `nag_alpha`, and `nag_sigma_end`.
  - Preserve and encode the negative prompt for NAG even when CFG is 1.
- The workflow intentionally uses the external `NAGGuider` custom node from
  `ChenDarYen/ComfyUI-NAG` or `BigStationW/ComfyUI-NAG-Extended`, replacing the
  normal `BasicGuider` path.
- The built-in ComfyUI `NAGuidance` model node is not used.
- If NAG is enabled and the external `NAGGuider` node is not installed, the
  workflow raises a clear setup error instead of silently falling back.

### Control Layers

- Added enabled/disabled toggles for control layers.
- Disabled control layers are omitted from generation metadata.
- Added hover tooltips explaining how each control layer behaves.
- Added post-process style control layers:
  - Color Match: helps preserve color and saturation from the source.
  - Light Map: screen-blends a highlight/light map back into the output.
- Color Match and Light Map expose percent strength in 5% steps.
- Flux 2 edit-model reference controls expose an experimental strength input:
  lower values blend Reference/Composition-style control images toward a blurred
  copy before they are encoded as reference latents.
- Flux 2 edit-model Reference/Composition strength is hidden by default and uses
  the advanced `Use custom values` strength slider when custom tuning is needed.
  The slider label shows percent values in 2% steps, matching the internal
  control precision.
- Flux 2 edit-model Reference/Composition defaults and non-custom state always
  use 100% reference strength.
- Added icons and tests for the new control modes.
- Control layers and their settings are now saved/restored with `.kra` project
  persistence for the edit/root control layer path.

### History and Metadata

- Cleaned up generation history tooltip metadata formatting.
- Added settings to hide/show prompt variants in history metadata:
  - Prompt
  - Prompt Evaluated
  - Prompt Final
- Text encoders now appear in history metadata when selected.
- Disabled control layers no longer show as active history metadata.
- New images are marked as new in history until selected.
- History grouping is less noisy: repeated generations with the same meaningful
  setup are grouped more compactly instead of splitting for every seed/tweak.
- History group headers now show a compact run summary with denoise, sampler,
  active LoRA count, and active control count.
- History thumbnails show a local-time timestamp badge in the lower-right
  corner.
- Added image comparison for two selected history images.
- Added metadata comparison for two selected history images, showing changed
  fields side-by-side with color-coded differences and an option to reveal
  unchanged fields.
- Metadata includes style, model, sampler, LoRAs, text encoders, control layers,
  denoise, seed, and prompt details in a cleaner layout.

### Clipboard Paste

- Added a Krita action for pasting clipboard images as a new layer.
- Wired the action so Ctrl+V can use the plugin paste path and avoid Krita's
  `blob:` URL clipboard error.

### Upscale and Utility Additions

- Added optional upscale noise injection controls.
- Added Whole Image refine mode for prompt-guided upscale/refine passes without
  splitting into tiles.
- Upscale/refine denoise now supports the full 0-100% range; 0% skips the
  diffusion refine pass, 100% runs full denoise.
- Added a Flux 2 Upscale - Euler A CFG++ sampler preset using
  `euler_ancestral_cfg_pp` with `sgm_uniform` scheduling.
- Added optional LoRA controls for single-pass upscale/refine workflows.
- Added a SeedVR2 Final Upscale section for running the external
  `ComfyUI-SeedVR2_VideoUpscaler` node after an image/refine pass.
  - Exposes DiT model, VAE model, device, DiT/VAE/tensor offload, attention
    mode, color correction, input/latent noise, BlockSwap, Swap I/O, VAE
    tiling, and debug logging.
  - Uses SeedVR2 as a separate final upscale action instead of forcing it into
    the normal Flux refine chain.
- Added VRAM display and a Free VRAM button in the sidebar.
- Added a managed-server Restart Comfy button next to Free VRAM:
  - Cancels active/queued plugin jobs locally.
  - Disconnects from ComfyUI.
  - Force-stops the managed ComfyUI process.
  - Starts the managed server again and reconnects.
  - The button is only enabled for the plugin-managed local server, not for
    external or cloud backends.
- Added progress detail plumbing from ComfyUI messages.
- Added several robustness patches around ComfyUI/Nunchaku startup warnings and
  Windows logging issues encountered during testing.

### Text Encoder Selection

- Added style-level text encoder override selection.
- Text encoder selections are passed into workflow model loading.
- Text encoder selections are included in history metadata.
- Missing text encoder warnings now account for selected overrides.

## Flux 2 Scheduled CFG Experiment

Scheduled CFG is implemented as an opt-in Flux 2 sampler preset experiment.

- The normal Flux 2 presets remain unchanged.
- Scheduled CFG requires ComfyUI Inspire Pack.
- If Inspire Pack is missing, the plugin reports a clear error when a Scheduled
  CFG preset is used.
- Scheduled CFG keeps a separate empty negative conditioning branch for Flux 2,
  even though the normal Flux 2 negative prompt UI remains disabled.
- The workflow routes sampling through:

```text
positive + negative/empty conditioning + sigmas
-> Scheduled CFGGuider (Inspire)
-> SamplerCustomAdvanced
```

- Added presets:
  - `Flux 2 - Euler Scheduled CFG`
- The default values are:

```text
from_cfg: 1.0
to_cfg: 1.5
schedule: exp
```

RES4LYF scheduled CFG presets are intentionally not exposed. RES4LYF samplers
can make additional model calls after the scheduled sigma list is exhausted,
which currently trips Inspire Pack's `ScheduledCFGGuider` with `KeyError: 9`.
There is an [upstream Inspire Pack report][inspire-res4lyf-scheduled-cfg]
for a similar RES4LYF + Scheduled CFGGuider failure.

[inspire-res4lyf-scheduled-cfg]: https://github.com/ltdrdata/ComfyUI-Inspire-Pack/issues/252

## Local Development

Restart Krita after Python/UI changes. The plugin is loaded by Krita at startup,
so most UI and workflow edits are not hot-reloaded.

Useful checks:

```powershell
python -m py_compile ai_diffusion\style.py ai_diffusion\server.py ai_diffusion\workflow.py ai_diffusion\ui\widget.py ai_diffusion\ui\style.py ai_diffusion\ui\control.py ai_diffusion\ui\generation.py ai_diffusion\ui\settings_widgets.py
python -m pytest tests\test_workflow.py tests\test_comfy_workflow.py tests\test_api.py tests\test_settings.py tests\test_jobs.py -q
git diff --check
```

Build/package commands from the upstream project still apply when needed:

```powershell
bun run build
bun run package
```

## User Data Locations

User styles are usually stored in:

```text
C:\Users\<user>\AppData\Roaming\krita\ai_diffusion\styles
```

Built-in/fork styles live in:

```text
ai_diffusion\styles
```

User settings are usually stored in:

```text
C:\Users\<user>\AppData\Roaming\krita\ai_diffusion\settings.json
```

## Upstream Project

The original project provides the core plugin, installer, documentation, and
model support:

- Upstream repository: https://github.com/Acly/krita-ai-diffusion
- User documentation: https://docs.interstice.cloud
- ComfyUI backend: https://github.com/comfyanonymous/ComfyUI

This fork is not a replacement for upstream documentation. It is a checkpoint of
the local Flux 2 / UI-tweaks work so the fork can be pushed before testing the
Scheduled CFG experiment.
