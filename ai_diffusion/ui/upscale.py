from PyQt5.QtCore import QEvent, QMetaObject, Qt, pyqtSignal
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import workflow
from ..jobs import JobKind
from ..localization import translate as _
from ..model import Model, TileOverlapMode, UpscaleRefineMode
from ..properties import Bind, Binding, bind, bind_combo, bind_toggle
from ..resources import ControlMode, UpscalerName
from ..root import root
from ..style import SamplerPresets
from . import theme
from .settings_widgets import NoWheelComboBox, WarningIcon
from .switch import SwitchWidget
from .theme import SignalBlocker, set_text_clipped
from .widget import (
    ErrorBox,
    GenerateButton,
    QueueButton,
    LoraDockerPanel,
    StrengthWidget,
    StyleSelectWidget,
    TextPromptWidget,
    WorkspaceSelectWidget,
)


class FactorWidget(QWidget):
    value_changed = pyqtSignal(float)

    def __init__(self, parent: QWidget | None):
        super().__init__(parent)
        self._value = 1.0

        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setMinimum(100)
        self.slider.setMaximum(400)
        self.slider.setTickInterval(50)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.setSingleStep(50)
        self.slider.setPageStep(50)
        self.slider.valueChanged.connect(self.change_factor_slider)

        self.input = QDoubleSpinBox(self)
        self.input.setMinimum(1.0)
        self.input.setMaximum(4.0)
        self.input.setSingleStep(0.5)
        self.input.setPrefix(_("Scale") + ": ")
        self.input.setSuffix("x")
        self.input.setDecimals(2)
        self.input.valueChanged.connect(self.change_factor)

        self.target_label = QLabel(self)
        self.target_label.setStyleSheet(f"color: {theme.grey};")

        value_layout = QHBoxLayout()
        value_layout.addWidget(self.slider)
        value_layout.addWidget(self.input)
        layout = QVBoxLayout(self)
        layout.addLayout(value_layout)
        layout.addWidget(self.target_label, alignment=Qt.AlignmentFlag.AlignRight)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value: float):
        if value != self._value:
            self._value = value
            with SignalBlocker(self.input), SignalBlocker(self.slider):
                self.slider.setValue(int(value * 100))
                self.input.setValue(value)
            self.update_target_extent()
            self.value_changed.emit(value)

    def change_factor_slider(self, value: float):
        rounded = round(value / 50) * 50
        if rounded != value:
            self.slider.setValue(rounded)
        else:
            self.value = value / 100

    def change_factor(self, value: float):
        self.value = value

    def update_target_extent(self):
        e = root.active_model.document.extent * self.value
        if self.slider.isSliderDown() or self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self.target_label.setText(_("Target size") + f": {e.width} x {e.height}")
        else:
            self.target_label.setText("")

    def enterEvent(self, a0: QEvent | None):
        self.update_target_extent()
        super().enterEvent(a0)

    def leaveEvent(self, a0: QEvent | None):
        self.update_target_extent()
        super().leaveEvent(a0)


class UpscaleWidget(QWidget):
    _model: Model
    _model_bindings: list[QMetaObject.Connection | Binding]

    def __init__(self):
        super().__init__()
        self._model = root.active_model
        self._model_bindings = []
        root.connection.state_changed.connect(self.update_models)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 4, 0)
        self.setLayout(layout)

        self.workspace_select = WorkspaceSelectWidget(self)
        self.model_select = QComboBox(self)
        model_layout = QHBoxLayout()
        model_layout.addWidget(self.workspace_select)
        model_layout.addWidget(self.model_select)
        layout.addLayout(model_layout)

        self.factor_widget = FactorWidget(self)
        self.factor_widget.value_changed.connect(self._update_factor)
        layout.addWidget(self.factor_widget)

        self.noise_checkbox = QCheckBox(_("Inject Noise"), self)
        self.noise_checkbox.setToolTip(
            _("Add random noise before upscaling to reduce blurriness with some models")
        )
        layout.addWidget(self.noise_checkbox)

        self.noise_slider = StrengthWidget(
            slider_range=(1, 30), prefix=False, parent=self
        )
        noise_layout = QHBoxLayout()
        noise_layout.addWidget(QLabel(_("Noise Strength"), self), 1)
        noise_layout.addWidget(self.noise_slider, 3)
        self._noise_layout_widget = QWidget(self)
        self._noise_layout_widget.setLayout(noise_layout)
        self._noise_layout_widget.setVisible(False)
        layout.addWidget(self._noise_layout_widget)

        self.refinement_checkbox = QGroupBox(_("Refine upscaled image"), self)
        self.refinement_checkbox.setCheckable(True)

        self.style_select = StyleSelectWidget(self)
        self.refine_mode_combo = NoWheelComboBox(self)
        self.refine_mode_combo.addItem(_("Tiled"), UpscaleRefineMode.tiled)
        self.refine_mode_combo.addItem(_("Whole Image"), UpscaleRefineMode.whole_image)
        self.refine_mode_combo.setToolTip(
            _(
                "Tiled uses less memory for large images. Whole Image refines the full image at once and preserves global context better."
            )
        )
        refine_mode_layout = QHBoxLayout()
        refine_mode_layout.addWidget(QLabel(_("Refine Mode"), self), 1)
        refine_mode_layout.addWidget(self.refine_mode_combo, 3)

        self.sampler_select = NoWheelComboBox(self)
        self.sampler_select.setToolTip(
            _(
                "Sampler preset used only for upscale/refine. Leave on Style Sampler to use the selected style's sampler."
            )
        )
        self._sync_sampler_presets()
        sampler_layout = QHBoxLayout()
        sampler_layout.addWidget(QLabel(_("Sampler"), self), 1)
        sampler_layout.addWidget(self.sampler_select, 3)

        self.strength_slider = StrengthWidget(slider_range=(0, 100), prefix=False, parent=self)
        strength_layout = QHBoxLayout()
        strength_layout.addWidget(QLabel(_("Denoise"), self), 1)
        strength_layout.addWidget(self.strength_slider, 3)

        self.unblur_slider = StrengthWidget(slider_range=(0, 100), prefix=False, parent=self)
        unblur_layout = QHBoxLayout()
        unblur_layout.addWidget(QLabel(_("Image guidance"), self), 1)
        unblur_layout.addWidget(self.unblur_slider, 3)
        root.connection.models_changed.connect(self._update_style)

        self.upscale_prompt = TextPromptWidget(line_count=2, parent=self)
        self.upscale_prompt.setPlaceholderText(
            _("Optional prompt used only for upscale/refine, e.g. 8K, intricate details")
        )
        self.upscale_prompt.setToolTip(
            _(
                "Optional prompt used only for upscale/refine. When set, it overrides the generation prompt for upscale jobs."
            )
        )
        prompt_edit_layout = QVBoxLayout()
        prompt_edit_layout.addWidget(QLabel(_("Upscale Prompt"), self))
        prompt_edit_layout.addWidget(self.upscale_prompt)

        self.lora_panel = LoraDockerPanel(self)

        self.overlap_custom_combo = QComboBox(self)
        self.overlap_custom_combo.addItem(_("Automatic"), TileOverlapMode.auto)
        self.overlap_custom_combo.addItem(_("Custom"), TileOverlapMode.custom)
        self.overlap_input = QSpinBox(self)
        self.overlap_input.setMinimum(0)
        self.overlap_input.setMaximum(128)
        self.overlap_input.setSingleStep(8)
        self.overlap_input.setSuffix(" px")
        self.overlap_input.setEnabled(False)
        overlap_layout = QHBoxLayout()
        overlap_layout.addWidget(QLabel(_("Tile Overlap"), self), 2)
        overlap_layout.addWidget(self.overlap_custom_combo)
        overlap_layout.addWidget(self.overlap_input)
        self.overlap_widget = QWidget(self)
        self.overlap_widget.setLayout(overlap_layout)

        self.use_prompt_switch = SwitchWidget(self)
        self.use_prompt_switch.toggled.connect(self._update_prompt)
        self.use_prompt_value = QLabel(_("Off"), self)
        self.prompt_warning = WarningIcon(self)
        self.prompt_label = QLabel(self)
        self.prompt_label.setMinimumWidth(40)
        prompt_layout = QHBoxLayout()
        prompt_layout.addWidget(QLabel(_("Use Generation Prompt"), self))
        prompt_layout.addWidget(self.prompt_label, 1)
        prompt_layout.addWidget(self.prompt_warning)
        prompt_layout.addWidget(self.use_prompt_value)
        prompt_layout.addWidget(self.use_prompt_switch)

        group_layout = QVBoxLayout(self.refinement_checkbox)
        group_layout.addWidget(self.style_select)
        group_layout.addLayout(refine_mode_layout)
        group_layout.addLayout(sampler_layout)
        group_layout.addLayout(strength_layout)
        group_layout.addLayout(unblur_layout)
        group_layout.addLayout(prompt_edit_layout)
        group_layout.addWidget(self.lora_panel)
        group_layout.addWidget(self.overlap_widget)
        group_layout.addLayout(prompt_layout)
        self.refinement_checkbox.setLayout(group_layout)
        layout.addWidget(self.refinement_checkbox)
        self.factor_widget.input.setMinimumWidth(self.strength_slider._input.width() + 10)

        self.seedvr2_group = QGroupBox(_("SeedVR2 Final Upscale"), self)
        self.seedvr2_dit_combo = NoWheelComboBox(self)
        self.seedvr2_vae_combo = NoWheelComboBox(self)
        self.seedvr2_device_combo = NoWheelComboBox(self)
        self.seedvr2_offload_combo = NoWheelComboBox(self)
        self.seedvr2_vae_offload_combo = NoWheelComboBox(self)
        self.seedvr2_tensor_offload_combo = NoWheelComboBox(self)
        self.seedvr2_attention_combo = NoWheelComboBox(self)
        for value in _seedvr2_attention_options:
            self.seedvr2_attention_combo.addItem(value, value)
        self.seedvr2_color_combo = NoWheelComboBox(self)
        for value, text in _seedvr2_color_options:
            self.seedvr2_color_combo.addItem(text, value)

        self.seedvr2_tile_auto = QCheckBox(_("Auto"), self)
        self.seedvr2_tile_auto.setToolTip(
            _(
                "Automatically choose the SeedVR2 crop grid from the target aspect ratio. "
                "Square images use 4x4; portrait images use rows x columns like 4x3."
            )
        )
        self.seedvr2_tile_rows_input = QSpinBox(self)
        self.seedvr2_tile_rows_input.setMinimum(1)
        self.seedvr2_tile_rows_input.setMaximum(16)
        self.seedvr2_tile_rows_input.setToolTip(
            _(
                "SeedVR2 crop-grid rows. The guide recommends 4x4 for square images; use 4x3 or 3x4 for non-square images."
            )
        )
        self.seedvr2_tile_columns_input = QSpinBox(self)
        self.seedvr2_tile_columns_input.setMinimum(1)
        self.seedvr2_tile_columns_input.setMaximum(16)
        self.seedvr2_tile_columns_input.setToolTip(
            _(
                "SeedVR2 crop-grid columns. The guide recommends 4x4 for square images; use 4x3 or 3x4 for non-square images."
            )
        )
        self.seedvr2_tile_overlap_input = QSpinBox(self)
        self.seedvr2_tile_overlap_input.setMinimum(0)
        self.seedvr2_tile_overlap_input.setMaximum(512)
        self.seedvr2_tile_overlap_input.setSingleStep(16)
        self.seedvr2_tile_overlap_input.setSuffix(" px")
        self.seedvr2_tile_overlap_input.setToolTip(
            _(
                "Extra source pixels included around each SeedVR2 crop before upscaling. Increase this to reduce visible grid seams."
            )
        )

        self.seedvr2_input_noise = QDoubleSpinBox(self)
        self.seedvr2_input_noise.setMinimum(0.0)
        self.seedvr2_input_noise.setMaximum(1.0)
        self.seedvr2_input_noise.setSingleStep(0.001)
        self.seedvr2_input_noise.setDecimals(3)
        self.seedvr2_input_noise.setToolTip(
            _("SeedVR2 input noise injection. Leave at 0 unless troubleshooting artifacts.")
        )
        self.seedvr2_latent_noise = QDoubleSpinBox(self)
        self.seedvr2_latent_noise.setMinimum(0.0)
        self.seedvr2_latent_noise.setMaximum(1.0)
        self.seedvr2_latent_noise.setSingleStep(0.001)
        self.seedvr2_latent_noise.setDecimals(3)
        self.seedvr2_latent_noise.setToolTip(
            _("SeedVR2 latent noise injection. Leave at 0 unless the output needs softening.")
        )
        self.seedvr2_blocks_input = QSpinBox(self)
        self.seedvr2_blocks_input.setMinimum(0)
        self.seedvr2_blocks_input.setMaximum(36)
        self.seedvr2_blocks_input.setToolTip(
            _("SeedVR2 BlockSwap blocks. 0 disables BlockSwap; higher values use less VRAM but run slower.")
        )
        self.seedvr2_swap_io = QCheckBox(_("Swap I/O"), self)
        self.seedvr2_swap_io.setToolTip(
            _("Also offload SeedVR2 input/output layers with BlockSwap to reduce VRAM usage.")
        )
        self.seedvr2_vae_tiled = QCheckBox(_("VAE Tiling"), self)
        self.seedvr2_vae_tiled.setToolTip(
            _("Use tiled SeedVR2 VAE encode/decode to reduce VRAM usage at high resolutions.")
        )
        self.seedvr2_vae_tile_size_input = QSpinBox(self)
        self.seedvr2_vae_tile_size_input.setMinimum(256)
        self.seedvr2_vae_tile_size_input.setMaximum(4096)
        self.seedvr2_vae_tile_size_input.setSingleStep(128)
        self.seedvr2_vae_tile_size_input.setSuffix(" px")
        self.seedvr2_vae_tile_size_input.setToolTip(
            _("SeedVR2 VAE tile size. Larger tiles can reduce seams but use more VRAM.")
        )
        self.seedvr2_vae_tile_overlap_input = QSpinBox(self)
        self.seedvr2_vae_tile_overlap_input.setMinimum(0)
        self.seedvr2_vae_tile_overlap_input.setMaximum(1024)
        self.seedvr2_vae_tile_overlap_input.setSingleStep(32)
        self.seedvr2_vae_tile_overlap_input.setSuffix(" px")
        self.seedvr2_vae_tile_overlap_input.setToolTip(
            _("SeedVR2 VAE tile overlap. More overlap can reduce seams but uses more memory.")
        )
        self.seedvr2_debug = QCheckBox(_("Debug"), self)
        self.seedvr2_debug.setToolTip(_("Enable detailed SeedVR2 memory/timing logs."))

        self.seedvr2_button = GenerateButton(JobKind.upscaling, self)
        self.seedvr2_button.operation = _("Run SeedVR2")
        self.seedvr2_button.clicked.connect(self.seedvr2_upscale)

        seedvr2_layout = QVBoxLayout(self.seedvr2_group)
        seedvr2_layout.addLayout(self._row(_("DiT Model"), self.seedvr2_dit_combo))
        seedvr2_layout.addLayout(self._row(_("VAE Model"), self.seedvr2_vae_combo))
        seedvr2_device_layout = QHBoxLayout()
        seedvr2_device_layout.addWidget(QLabel(_("Device"), self), 1)
        seedvr2_device_layout.addWidget(self.seedvr2_device_combo, 1)
        seedvr2_device_layout.addWidget(QLabel(_("DiT Offload"), self), 1)
        seedvr2_device_layout.addWidget(self.seedvr2_offload_combo, 1)
        seedvr2_layout.addLayout(seedvr2_device_layout)
        seedvr2_offload_layout = QHBoxLayout()
        seedvr2_offload_layout.addWidget(QLabel(_("VAE Offload"), self), 1)
        seedvr2_offload_layout.addWidget(self.seedvr2_vae_offload_combo, 1)
        seedvr2_offload_layout.addWidget(QLabel(_("Tensor Offload"), self), 1)
        seedvr2_offload_layout.addWidget(self.seedvr2_tensor_offload_combo, 1)
        seedvr2_layout.addLayout(seedvr2_offload_layout)
        seedvr2_layout.addLayout(self._row(_("Attention"), self.seedvr2_attention_combo))
        seedvr2_layout.addLayout(self._row(_("Color"), self.seedvr2_color_combo))
        seedvr2_grid_layout = QHBoxLayout()
        seedvr2_grid_layout.addWidget(QLabel(_("Crop Grid"), self), 1)
        seedvr2_grid_layout.addWidget(self.seedvr2_tile_auto)
        seedvr2_grid_layout.addWidget(self.seedvr2_tile_rows_input, 1)
        seedvr2_grid_layout.addWidget(QLabel("x", self))
        seedvr2_grid_layout.addWidget(self.seedvr2_tile_columns_input, 1)
        seedvr2_layout.addLayout(seedvr2_grid_layout)
        seedvr2_crop_overlap_layout = QHBoxLayout()
        seedvr2_crop_overlap_layout.addWidget(QLabel(_("Crop Overlap"), self), 1)
        seedvr2_crop_overlap_layout.addWidget(self.seedvr2_tile_overlap_input, 3)
        seedvr2_layout.addLayout(seedvr2_crop_overlap_layout)
        seedvr2_noise_layout = QHBoxLayout()
        seedvr2_noise_layout.addWidget(QLabel(_("Input Noise"), self), 1)
        seedvr2_noise_layout.addWidget(self.seedvr2_input_noise, 1)
        seedvr2_noise_layout.addWidget(QLabel(_("Latent Noise"), self), 1)
        seedvr2_noise_layout.addWidget(self.seedvr2_latent_noise, 1)
        seedvr2_layout.addLayout(seedvr2_noise_layout)
        seedvr2_options_layout = QHBoxLayout()
        seedvr2_options_layout.addWidget(QLabel(_("BlockSwap"), self))
        seedvr2_options_layout.addWidget(self.seedvr2_blocks_input)
        seedvr2_options_layout.addWidget(self.seedvr2_swap_io)
        seedvr2_options_layout.addWidget(self.seedvr2_vae_tiled)
        seedvr2_options_layout.addWidget(self.seedvr2_debug)
        seedvr2_layout.addLayout(seedvr2_options_layout)
        seedvr2_tile_layout = QHBoxLayout()
        seedvr2_tile_layout.addWidget(QLabel(_("VAE Tile"), self), 1)
        seedvr2_tile_layout.addWidget(self.seedvr2_vae_tile_size_input, 1)
        seedvr2_tile_layout.addWidget(QLabel(_("VAE Overlap"), self), 1)
        seedvr2_tile_layout.addWidget(self.seedvr2_vae_tile_overlap_input, 1)
        seedvr2_layout.addLayout(seedvr2_tile_layout)

        self.seedvr2_target_label = QLabel(self)
        self.seedvr2_target_label.setStyleSheet(f"color: {theme.grey}; font-size: 11px;")
        self.seedvr2_target_label.setWordWrap(True)
        seedvr2_layout.addWidget(
            self.seedvr2_target_label, alignment=Qt.AlignmentFlag.AlignRight
        )
        seedvr2_layout.addWidget(self.seedvr2_button)
        layout.addWidget(self.seedvr2_group)

        self.upscale_button = GenerateButton(JobKind.upscaling, self)
        self.upscale_button.operation = _("Upscale")
        self.upscale_button.clicked.connect(self.upscale)

        self.queue_button = QueueButton(supports_batch=False, parent=self)
        self.queue_button.setFixedHeight(self.upscale_button.height() - 2)

        actions_layout = QHBoxLayout()
        actions_layout.addWidget(self.upscale_button)
        actions_layout.addWidget(self.queue_button)
        layout.addLayout(actions_layout)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        layout.addWidget(self.progress_bar)

        self.progress_detail = QLabel(self)
        self.progress_detail.setStyleSheet(f"color: {theme.grey}; font-size: 11px;")
        self.progress_detail.setVisible(False)
        layout.addWidget(self.progress_detail)

        self.error_box = ErrorBox(self)
        layout.addWidget(self.error_box)

        layout.addStretch()

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        if self._model != model:
            Binding.disconnect_all(self._model_bindings)
            self._model = model
            self._model_bindings = [
                bind(model, "workspace", self.workspace_select, "value", Bind.one_way),
                bind_combo(model.upscale, "upscaler", self.model_select),
                bind(model.upscale, "factor", self.factor_widget, "value"),
                bind_toggle(model.upscale, "inject_noise", self.noise_checkbox),
                bind(model.upscale, "noise_strength", self.noise_slider, "value"),
                bind_toggle(model.upscale, "use_diffusion", self.refinement_checkbox),
                bind_combo(model.upscale, "refine_mode", self.refine_mode_combo),
                bind(model, "style", self.style_select, "value"),
                bind_combo(model.upscale, "sampler_preset", self.sampler_select),
                bind(model.upscale, "strength", self.strength_slider, "value"),
                bind(model.upscale, "unblur_strength", self.unblur_slider, "value"),
                bind(model.upscale, "prompt", self.upscale_prompt, "text"),
                bind_combo(model.upscale, "tile_overlap_mode", self.overlap_custom_combo),
                bind(model.upscale, "tile_overlap", self.overlap_input, "value"),
                bind_toggle(model.upscale, "use_prompt", self.use_prompt_switch),
                bind_combo(model.upscale, "seedvr2_dit_model", self.seedvr2_dit_combo),
                bind_combo(model.upscale, "seedvr2_vae_model", self.seedvr2_vae_combo),
                bind_combo(model.upscale, "seedvr2_device", self.seedvr2_device_combo),
                bind_combo(model.upscale, "seedvr2_dit_offload_device", self.seedvr2_offload_combo),
                bind_combo(
                    model.upscale, "seedvr2_vae_offload_device", self.seedvr2_vae_offload_combo
                ),
                bind_combo(
                    model.upscale,
                    "seedvr2_tensor_offload_device",
                    self.seedvr2_tensor_offload_combo,
                ),
                bind_combo(model.upscale, "seedvr2_attention_mode", self.seedvr2_attention_combo),
                bind_combo(model.upscale, "seedvr2_color_correction", self.seedvr2_color_combo),
                bind(
                    model.upscale, "seedvr2_input_noise_scale", self.seedvr2_input_noise, "value"
                ),
                bind(
                    model.upscale, "seedvr2_latent_noise_scale", self.seedvr2_latent_noise, "value"
                ),
                bind(model.upscale, "seedvr2_blocks_to_swap", self.seedvr2_blocks_input, "value"),
                bind_toggle(model.upscale, "seedvr2_swap_io_components", self.seedvr2_swap_io),
                bind_toggle(model.upscale, "seedvr2_vae_tiled", self.seedvr2_vae_tiled),
                bind_toggle(model.upscale, "seedvr2_tile_auto", self.seedvr2_tile_auto),
                bind(
                    model.upscale,
                    "seedvr2_vae_tile_size",
                    self.seedvr2_vae_tile_size_input,
                    "value",
                ),
                bind(
                    model.upscale,
                    "seedvr2_vae_tile_overlap",
                    self.seedvr2_vae_tile_overlap_input,
                    "value",
                ),
                bind(
                    model.upscale,
                    "seedvr2_tile_rows",
                    self.seedvr2_tile_rows_input,
                    "value",
                ),
                bind(
                    model.upscale,
                    "seedvr2_tile_columns",
                    self.seedvr2_tile_columns_input,
                    "value",
                ),
                bind(
                    model.upscale,
                    "seedvr2_tile_overlap",
                    self.seedvr2_tile_overlap_input,
                    "value",
                ),
                bind_toggle(model.upscale, "seedvr2_enable_debug", self.seedvr2_debug),
                bind(model.upscale, "can_generate", self.upscale_button, "enabled", Bind.one_way),
                bind(model, "error", self.error_box, "error", Bind.one_way),
                model.upscale.tile_overlap_mode_changed.connect(self._update_overlap),
                model.upscale.refine_mode_changed.connect(self._update_refine_mode),
                model.upscale.use_prompt_changed.connect(self._update_prompt),
                model.regions.modified.connect(self._update_prompt),
                model.regions.added.connect(self._update_prompt),
                model.regions.removed.connect(self._update_prompt),
                model.progress_changed.connect(self.update_progress),
                model.progress_details_changed.connect(self.update_progress),
                model.style_changed.connect(self._update_style),
                root.connection.models_changed.connect(self._sync_seedvr2_options),
                model.upscale.can_generate_changed.connect(self._update_seedvr2_status),
                model.upscale.target_extent_changed.connect(self._update_seedvr2_status),
                model.upscale.seedvr2_tile_auto_changed.connect(self._update_seedvr2_status),
                model.upscale.seedvr2_tile_rows_changed.connect(self._update_seedvr2_status),
                model.upscale.seedvr2_tile_columns_changed.connect(self._update_seedvr2_status),
                model.upscale.seedvr2_tile_auto_changed.connect(
                    self._update_seedvr2_tile_options
                ),
                model.upscale.seedvr2_vae_tiled_changed.connect(
                    self._update_seedvr2_tile_options
                ),
            ]
            self.upscale_button.model = model
            self.seedvr2_button.model = model
            self.queue_button.model = model
            self.lora_panel.model = model
            self.noise_checkbox.toggled.connect(self._noise_layout_widget.setVisible)
            self._noise_layout_widget.setVisible(model.upscale.inject_noise)
            self._update_prompt()
            self._update_style()
            self._update_overlap()
            self._update_refine_mode()
            self._sync_seedvr2_options()
            self._update_seedvr2_tile_options()
            self.update_progress()

    def update_models(self):
        if client := root.connection.client_if_connected:
            with SignalBlocker(self.model_select):
                self.model_select.clear()
                for file in sorted(client.models.upscalers, key=_upscaler_order):
                    if file == UpscalerName.default.value:
                        name = f"Default ({file.removesuffix('.pth')})"
                        self.model_select.addItem(name, file)
                    elif file == UpscalerName.fast_4x.value:
                        name = f"Fast ({file.removesuffix('.safetensors')})"
                        self.model_select.addItem(name, file)
                    elif file == UpscalerName.quality.value:
                        name = f"Quality ({file.removesuffix('.pth')})"
                        self.model_select.addItem(name, file)
                    elif file == UpscalerName.sharp.value:
                        name = f"Sharp ({file.removesuffix('.pth')})"
                        self.model_select.addItem(name, file)
                    elif file in [UpscalerName.fast_2x.value, UpscalerName.fast_3x.value]:
                        pass
                    else:
                        self.model_select.addItem(file, file)
                selected = self.model_select.findData(self.model.upscale.upscaler)
                self.model_select.setCurrentIndex(max(selected, 0))
        self._sync_seedvr2_options()

    def _sync_sampler_presets(self):
        self.sampler_select.clear()
        self.sampler_select.addItem(_("Style Sampler"), "")
        for name in SamplerPresets.instance().names():
            self.sampler_select.addItem(name, name)

    def _row(self, label: str, widget: QWidget):
        row = QHBoxLayout()
        row.addWidget(QLabel(label, self), 1)
        row.addWidget(widget, 3)
        return row

    def _sync_combo_options(self, combo: QComboBox, options: list[str], current: str):
        with SignalBlocker(combo):
            combo.clear()
            for option in options:
                combo.addItem(option, option)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else 0)

    def _seedvr2_options(self, node: str, input_name: str, fallback: list[str]):
        if client := root.connection.client_if_connected:
            options = client.models.node_inputs.options(node, input_name)
            if options:
                return options
        return fallback

    def _sync_seedvr2_options(self):
        self._sync_combo_options(
            self.seedvr2_dit_combo,
            self._seedvr2_options("SeedVR2LoadDiTModel", "model", _seedvr2_dit_models),
            self.model.upscale.seedvr2_dit_model,
        )
        self._sync_combo_options(
            self.seedvr2_vae_combo,
            self._seedvr2_options("SeedVR2LoadVAEModel", "model", _seedvr2_vae_models),
            self.model.upscale.seedvr2_vae_model,
        )
        device_options = self._seedvr2_options(
            "SeedVR2LoadDiTModel", "device", _seedvr2_devices
        )
        self._sync_combo_options(
            self.seedvr2_device_combo, device_options, self.model.upscale.seedvr2_device
        )
        offload_options = ["none", "cpu"] + [d for d in device_options if d != "cpu"]
        self._sync_combo_options(
            self.seedvr2_offload_combo,
            offload_options,
            self.model.upscale.seedvr2_dit_offload_device,
        )
        self._sync_combo_options(
            self.seedvr2_vae_offload_combo,
            offload_options,
            self.model.upscale.seedvr2_vae_offload_device,
        )
        self._sync_combo_options(
            self.seedvr2_tensor_offload_combo,
            offload_options,
            self.model.upscale.seedvr2_tensor_offload_device,
        )
        self._update_seedvr2_status()

    def _update_seedvr2_status(self):
        installed = False
        if client := root.connection.client_if_connected:
            installed = "SeedVR2VideoUpscaler" in client.models.node_inputs
        self.seedvr2_button.setEnabled(self.model.upscale.can_generate and installed)
        target = self.model.upscale.target_extent
        factor = self.model.upscale.factor
        if factor <= 1.0:
            self.seedvr2_target_label.setStyleSheet(
                f"color: {theme.yellow}; font-size: 11px;"
            )
        else:
            self.seedvr2_target_label.setStyleSheet(f"color: {theme.grey}; font-size: 11px;")
        if self.model.upscale.seedvr2_tile_auto:
            rows, columns = workflow.seedvr2_auto_tile_grid(target)
            grid_text = _("auto") + f" {rows}x{columns}"
        else:
            grid_text = (
                f"{self.model.upscale.seedvr2_tile_rows}x"
                f"{self.model.upscale.seedvr2_tile_columns}"
            )
        self.seedvr2_target_label.setText(
            _("Target")
            + f": {target.width} x {target.height} "
            + f"({factor:.1f}x, {grid_text} "
            + _("tiles")
            + ")"
        )
        if installed:
            self.seedvr2_button.setToolTip(
                _("Uses the current Scale value.")
                + f" {target.width} x {target.height} ({factor:.1f}x)"
            )
        else:
            self.seedvr2_button.setToolTip(
                _("Install ComfyUI-SeedVR2_VideoUpscaler and restart the server to enable this.")
            )

    def update_progress(self):
        progress = self.model.progress
        details = self.model.progress_details
        detail_text = _progress_detail_text(
            details.current_node, details.sample_step, details.sample_max
        )

        busy = progress < 0 or (
            details.current_node in _seedvr2_progress_nodes
            and details.sample_max == 0
            and progress < 1.0
        )
        if busy:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(max(0, min(100, int(progress * 100))))

        self.progress_detail.setText(detail_text)
        self.progress_detail.setVisible(bool(detail_text))

    def upscale(self):
        self.model.upscale_image()

    def seedvr2_upscale(self):
        self.model.seedvr2_upscale_image()

    def _update_overlap(self):
        self.overlap_input.setEnabled(
            self.model.upscale.tile_overlap_mode is TileOverlapMode.custom
        )

    def _update_refine_mode(self):
        is_tiled = self.model.upscale.refine_mode is UpscaleRefineMode.tiled
        self.overlap_widget.setVisible(is_tiled)

    def _update_seedvr2_tile_options(self):
        vae_enabled = self.model.upscale.seedvr2_vae_tiled
        self.seedvr2_vae_tile_size_input.setEnabled(vae_enabled)
        self.seedvr2_vae_tile_overlap_input.setEnabled(vae_enabled)
        grid_enabled = not self.model.upscale.seedvr2_tile_auto
        self.seedvr2_tile_rows_input.setEnabled(grid_enabled)
        self.seedvr2_tile_columns_input.setEnabled(grid_enabled)

    def _update_style(self):
        arch = self.model.arch
        if arch.is_edit:
            tooltip = _("Not supported for edit models")
            self.strength_slider.setEnabled(False)
            self.strength_slider.setToolTip(tooltip)
            self.unblur_slider.setEnabled(False)
            self.unblur_slider.setToolTip(tooltip)
        else:
            has_unblur = False
            if client := root.connection.client_if_connected:
                models = client.models.for_arch(self.model.arch)
                has_unblur = models.find_control(ControlMode.blur) is not None
            self.unblur_slider.setEnabled(has_unblur)
            if not has_unblur:
                tooltip = _("The tile/unblur control model is not installed.")
            else:
                tooltip = _(
                    "When enabled, the low resolution image is used as guidance for refining the upscaled image.\nThis produces results which are closer to the original while enhancing local details."
                )
            self.unblur_slider.setToolTip(tooltip)

    def _update_prompt(self):
        self.use_prompt_value.setText(_("On") if self.model.upscale.use_prompt else _("Off"))
        text = self.model.regions.positive
        if len(self.model.regions) > 0:
            text = f"<b>{len(self.model.regions)} " + _("Regions") + f"</b> | {text}"
        padding = 8
        if self.model.upscale.use_prompt and len(self.model.regions) == 0:
            padding += self.prompt_warning.icon_size
            self.prompt_warning.show_message(
                _(
                    "Text prompt regions have not been set up.\nIt is not recommended to use a single text description for tiled upscale,\nunless it can be generally applied to all parts of the image."
                )
            )
        else:
            self.prompt_warning.hide()
        set_text_clipped(self.prompt_label, text, padding=padding)

    def _update_factor(self):
        if self.factor_widget.value == 1.0 and self.model.upscale.use_diffusion:
            self.upscale_button.operation = _("Refine")
        else:
            self.upscale_button.operation = _("Upscale")
        self._update_seedvr2_status()


def _upscaler_order(filename: str):
    return {
        UpscalerName.default.value: 0,
        UpscalerName.fast_4x.value: 1,
        UpscalerName.quality.value: 2,
        UpscalerName.sharp.value: 3,
    }.get(filename, 99)


_seedvr2_dit_models = [
    "seedvr2_ema_3b_fp8_e4m3fn.safetensors",
    "seedvr2_ema_3b_fp16.safetensors",
    "seedvr2_ema_3b-Q8_0.gguf",
    "seedvr2_ema_3b-Q4_K_M.gguf",
    "seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors",
    "seedvr2_ema_7b-Q4_K_M.gguf",
    "seedvr2_ema_7b_sharp_fp8_e4m3fn_mixed_block35_fp16.safetensors",
    "seedvr2_ema_7b_sharp-Q4_K_M.gguf",
]

_seedvr2_vae_models = ["ema_vae_fp16.safetensors"]
_seedvr2_devices = ["cuda:0", "cpu"]
_seedvr2_attention_options = [
    "sdpa",
    "flash_attn_2",
    "flash_attn_3",
    "sageattn_2",
    "sageattn_3",
]
_seedvr2_color_options = [
    ("lab", "LAB"),
    ("wavelet", "Wavelet"),
    ("wavelet_adaptive", "Wavelet Adaptive"),
    ("hsv", "HSV"),
    ("adain", "AdaIN"),
    ("none", _("None")),
]

_seedvr2_progress_nodes = {
    "SeedVR2LoadDiTModel": _("SeedVR2: preparing DiT model"),
    "SeedVR2LoadVAEModel": _("SeedVR2: preparing VAE model"),
    "SeedVR2VideoUpscaler": _("SeedVR2: upscaling, decoding, and color matching"),
}


def _progress_detail_text(current_node: str, sample_step: int, sample_max: int):
    if not current_node:
        return ""
    text = _seedvr2_progress_nodes.get(current_node, current_node)
    if sample_step > 0 and sample_max > 0:
        text += f"  {sample_step}/{sample_max}"
    return text
