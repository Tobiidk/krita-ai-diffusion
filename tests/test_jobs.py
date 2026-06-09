from types import SimpleNamespace

from ai_diffusion.image import Bounds
from ai_diffusion.jobs import JobParams


def _control(enabled=True, is_supported=True, name="Layer", uses_edit_reference_strength=False):
    return SimpleNamespace(
        enabled=enabled,
        is_supported=is_supported,
        uses_edit_reference_strength=uses_edit_reference_strength,
        mode=SimpleNamespace(
            text="Composition", has_timestep_range=True, is_post_processing=False
        ),
        strength=50,
        strength_multiplier=100,
        post_strength_percent=70,
        layer=SimpleNamespace(name=name),
        start=0.0,
        end=1.0,
    )


def test_set_control_omits_disabled_and_unsupported_layers():
    params = JobParams(Bounds(0, 0, 1, 1), "test")

    params.set_control([
        _control(name="Enabled"),
        _control(enabled=False, name="Disabled"),
        _control(is_supported=False, name="Unsupported"),
    ])

    assert params.metadata["control"] == [
        {
            "mode": "Composition",
            "strength": 0.5,
            "image": "Enabled",
            "start": 0.0,
            "end": 1.0,
        }
    ]


def test_set_control_omits_range_for_post_process_layers():
    params = JobParams(Bounds(0, 0, 1, 1), "test")
    control = _control(name="Reference")
    control.mode.has_timestep_range = False

    params.set_control([control])

    assert params.metadata["control"] == [
        {
            "mode": "Composition",
            "strength": 0.5,
            "image": "Reference",
        }
    ]


def test_set_control_omits_range_for_edit_reference_controls():
    params = JobParams(Bounds(0, 0, 1, 1), "test")
    control = _control(name="Reference", uses_edit_reference_strength=True)

    params.set_control([control])

    assert params.metadata["control"] == [
        {
            "mode": "Composition",
            "strength": 0.5,
            "image": "Reference",
        }
    ]


def test_set_control_uses_percent_strength_for_post_process_layers():
    params = JobParams(Bounds(0, 0, 1, 1), "test")
    control = _control(name="Light")
    control.mode.has_timestep_range = False
    control.mode.is_post_processing = True
    control.post_strength_percent = 35

    params.set_control([control])

    assert params.metadata["control"] == [
        {
            "mode": "Composition",
            "strength": 0.35,
            "image": "Light",
        }
    ]


def test_set_style_records_resolved_text_encoders():
    params = JobParams(Bounds(0, 0, 1, 1), "test")
    style = SimpleNamespace(
        filename="style.json",
        sampler="sampler",
        sampler_steps=20,
        cfg_scale=3.5,
        nag_enabled=False,
        text_encoders={},
    )

    params.set_style(style, "model.safetensors", {"qwen_3_4b": "encoder.safetensors"})

    assert params.metadata["text_encoders"] == {"qwen_3_4b": "encoder.safetensors"}


def test_set_style_records_nag_settings():
    params = JobParams(Bounds(0, 0, 1, 1), "test")
    style = SimpleNamespace(
        filename="style.json",
        sampler="sampler",
        sampler_steps=20,
        cfg_scale=1.0,
        nag_enabled=True,
        nag_scale=8.0,
        nag_tau=2.5,
        nag_alpha=0.25,
        nag_sigma_end=0.75,
        text_encoders={},
    )

    params.set_style(style, "model.safetensors")

    assert params.metadata["nag"] == {
        "scale": 8.0,
        "tau": 2.5,
        "alpha": 0.25,
        "sigma_end": 0.75,
    }
