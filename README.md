# Krita AI Diffusion - Tobii UI Tweaks Fork

This fork is a working branch of
[Acly/krita-ai-diffusion](https://github.com/Acly/krita-ai-diffusion) focused on
Flux 2 / Klein workflows, faster sidebar control, clearer history metadata, and
more practical LoRA handling inside Krita.

The local checkout is intended to be symlinked into Krita's `pykrita` plugin
folder, so changes in this repo can be tested directly in the installed plugin
after restarting Krita.

## Current Branch

- Remote: `https://github.com/Tobiidk/krita-ai-diffusion.git`
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
- The next planned experiment is an optional Flux 2 Scheduled CFGGuider path via
  ComfyUI Inspire Pack.

### Guidance, Steps, and Denoise

- Added sidebar sampler controls for steps and guidance where the active model
  architecture actually supports them.
- Kept Flux 2 guidance hidden in the sidebar because regular CFG was not wired
  to an effective Flux 2 node.
- Reworked denoise handling so edit denoise is not just a misleading bundled
  steps display.
- History metadata now records denoise, actual steps, and total steps when
  applicable.

### Sidebar LoRA Controls

- Added compact LoRA quick toggles in the generation sidebar.
- LoRA entries can be enabled/disabled directly from the sidebar.
- LoRA strength can be adjusted directly from the sidebar.
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

### Quick Generation Styles

- Added quick generation style buttons in the sidebar.
- Quick style generation keeps the selected quick style's model/checkpoint/text
  encoders but inherits current LoRAs and sampling settings from the active
  style.
- Quick style buttons now use a horizontal scroll strip instead of expanding or
  squeezing the sidebar.
- Quick style add/remove saves immediately.

### Control Layers

- Added enabled/disabled toggles for control layers.
- Disabled control layers are omitted from generation metadata.
- Added hover tooltips explaining how each control layer behaves.
- Added post-process style control layers:
  - Color Match: helps preserve color and saturation from the source.
  - Light Map: screen-blends a highlight/light map back into the output.
- Color Match and Light Map expose percent strength in 5% steps.
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
- Added image comparison for two selected history images.
- Metadata includes style, model, sampler, LoRAs, text encoders, control layers,
  denoise, seed, and prompt details in a cleaner layout.

### Clipboard Paste

- Added a Krita action for pasting clipboard images as a new layer.
- Wired the action so Ctrl+V can use the plugin paste path and avoid Krita's
  `blob:` URL clipboard error.

### Upscale and Utility Additions

- Added optional upscale noise injection controls.
- Added VRAM display and a Free VRAM button in the sidebar.
- Added progress detail plumbing from ComfyUI messages.
- Added several robustness patches around ComfyUI/Nunchaku startup warnings and
  Windows logging issues encountered during testing.

### Text Encoder Selection

- Added style-level text encoder override selection.
- Text encoder selections are passed into workflow model loading.
- Text encoder selections are included in history metadata.
- Missing text encoder warnings now account for selected overrides.

## Current Scheduled CFG Plan

Scheduled CFG is not implemented yet in this checkpoint.

The planned experiment is:

- Detect whether ComfyUI Inspire Pack exposes `ScheduledCFGGuider`.
- Add a Flux 2 only scheduled CFG path.
- Route Flux 2 sampling through:

```text
positive + negative/empty conditioning + sigmas
-> Scheduled CFGGuider (Inspire)
-> SamplerCustomAdvanced
```

- Start with the reported values:

```text
from_cfg: 1.0
to_cfg: 1.5
schedule: exp
```

This should be compatible with RES4LYF in principle because RES4LYF is the
sampler input and Scheduled CFG is the guider input to `SamplerCustomAdvanced`.

## Local Development

Restart Krita after Python/UI changes. The plugin is loaded by Krita at startup,
so most UI and workflow edits are not hot-reloaded.

Useful checks:

```powershell
python -m py_compile ai_diffusion\ui\widget.py ai_diffusion\ui\style.py ai_diffusion\ui\settings_widgets.py
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
