from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

from PyQt5.QtCore import QObject, Qt, QUuid, pyqtSignal

from . import jobs, model, resources, util
from .api import ControlInput
from .image import Bounds, Extent, Image
from .layer import Layer, LayerType
from .localization import translate as _
from .properties import ObservableProperties, Property
from .resources import Arch, ControlMode, ResourceKind, resource_id
from .util import PluginError
from .util import client_logger as log


class ControlLayer(QObject, ObservableProperties):
    max_preset_value = 4
    strength_multiplier = 50
    clip_vision_extent = Extent(224, 224)

    mode = Property(ControlMode.reference, persist=True, setter="set_mode")
    layer_id = Property(QUuid(), persist=True)
    enabled = Property(True, persist=True)
    preset_value = Property(2, persist=True, setter="set_preset_value")
    post_strength_percent = Property(70, persist=True, setter="set_post_strength_percent")
    strength = Property(50, persist=True)
    start = Property(0.0, persist=True)
    end = Property(1.0, persist=True)
    use_custom_strength = Property(False, persist=True, setter="set_use_custom_strength")
    is_supported = Property(True)
    is_pose_vector = Property(False)
    can_generate = Property(True)
    has_range = Property(True)
    has_active_job = Property(False)
    error_text = Property("")

    mode_changed = pyqtSignal(ControlMode)
    layer_id_changed = pyqtSignal(QUuid)
    enabled_changed = pyqtSignal(bool)
    preset_value_changed = pyqtSignal(int)
    post_strength_percent_changed = pyqtSignal(int)
    strength_changed = pyqtSignal(int)
    start_changed = pyqtSignal(float)
    end_changed = pyqtSignal(float)
    use_custom_strength_changed = pyqtSignal(bool)
    is_supported_changed = pyqtSignal(bool)
    is_pose_vector_changed = pyqtSignal(bool)
    can_generate_changed = pyqtSignal(bool)
    has_active_job_changed = pyqtSignal(bool)
    has_range_changed = pyqtSignal(bool)
    error_text_changed = pyqtSignal(str)
    modified = pyqtSignal(QObject, str)

    def __init__(self, model: model.Model, mode: ControlMode, layer_id: QUuid, index: int):
        from .root import root

        super().__init__()
        self._model = model
        self._index = index
        self._generate_job: jobs.Job | None = None
        self.layer_id = layer_id
        self.mode = mode
        self._update_is_supported()

        self.mode_changed.connect(self._update_is_supported)
        model.style_changed.connect(self._update_is_supported)
        model.edit_mode_changed.connect(self._update_is_supported)
        root.connection.state_changed.connect(self._update_is_supported)
        self.layer_id_changed.connect(self._update_is_pose_vector)
        model.jobs.job_finished.connect(self._update_active_job)

    @property
    def layer(self):
        layer = self._model.layers.updated().find(self.layer_id)
        assert layer is not None, "Control layer has been deleted"
        return layer

    def set_mode(self, mode: ControlMode):
        if mode != self.mode:
            self._mode = mode
            self.mode_changed.emit(mode)
            self._update_is_pose_vector()
            if not self.use_custom_strength:
                self._set_values_from_preset()

    def set_preset_value(self, value: int):
        if value != self.preset_value:
            self._preset_value = value
            self.preset_value_changed.emit(value)
            self._set_values_from_preset()

    def set_post_strength_percent(self, value: int):
        value = max(0, min(100, int(value)))
        if value != self.post_strength_percent:
            self._post_strength_percent = value
            self.post_strength_percent_changed.emit(value)
            self.modified.emit(self, "post_strength_percent")

    def _set_values_from_preset(self):
        params = ControlPresets.instance().interpolate(
            self.mode, self._model.arch, self.preset_value / self.max_preset_value
        )
        if self.mode.is_post_processing:
            self.post_strength_percent = round(params.strength * 100)
        self.strength = int(params.strength * self.strength_multiplier)
        self.start, self.end = params.range

    def set_use_custom_strength(self, value: bool):
        if value != self.use_custom_strength:
            self._use_custom_strength = value
            self.use_custom_strength_changed.emit(value)
            if not value:
                self._set_values_from_preset()

    @property
    def index(self):
        return self._index

    @index.setter
    def index(self, index: int):
        self._index = index
        self._update_is_supported()

    def to_api(self, bounds: Bounds | None = None, time: int | None = None):
        assert self.is_supported, "Control layer is not supported"
        extent = bounds.extent if bounds else self._model.document.extent
        layer = self.layer
        if self.mode.is_ip_adapter and not layer.bounds.is_zero:
            bounds = None  # ignore mask bounds, use layer bounds

        image = layer.get_pixels(bounds, time)

        if self.mode.is_lines or self.mode is ControlMode.stencil:
            image.make_opaque(background=Qt.GlobalColor.white)

        if self.mode.is_ip_adapter:
            if self._model.arch.supports_edit:
                if image.extent.height > extent.height:
                    w = (image.extent.width * extent.height) // image.extent.height
                    image = Image.scale(image, Extent(w, extent.height))
            else:
                image = Image.scale(image, self.clip_vision_extent)

        strength = (
            self.post_strength_percent / 100
            if self.mode.is_post_processing
            else self.strength / self.strength_multiplier
        )
        return ControlInput(self.mode, image, strength, (self.start, self.end))

    def generate(self):
        self._generate_job = self._model.generate_control_layer(self)
        self.has_active_job = True

    def _update_is_supported(self):
        from .root import root

        is_supported = True
        self.has_range = self.mode.has_timestep_range
        if client := root.connection.client_if_connected:
            models = client.models.for_arch(self._model.arch)

            if self.mode.is_ip_adapter and models.arch in [Arch.illu, Arch.illu_v]:
                resid = resource_id(ResourceKind.clip_vision, Arch.illu, "ip_adapter")
                has_clip_vision = client.models.resources.get(resid, None) is not None
                if not has_clip_vision:
                    search = resources.search_path(
                        ResourceKind.clip_vision, Arch.illu, "ip_adapter"
                    )
                    self.error_text = _("The server is missing the ClipVision model") + f" {search}"
                    is_supported = False

            if self.mode.is_ip_adapter and models.arch.supports_edit:
                is_supported = True  # Reference images are merged into the conditioning context
            elif self.mode.is_ip_adapter and models.ip_adapter.find(self.mode) is None:
                search_path = resources.search_path(ResourceKind.ip_adapter, models.arch, self.mode)
                if search_path:
                    self.error_text = (
                        _("The server is missing the IP-Adapter model") + f" {self.mode.text}"
                    )
                else:
                    self.error_text = _("Not supported for") + f" {models.arch.value}"
                if not client.features.ip_adapter:
                    self.error_text = _("IP-Adapter is not supported by this GPU")
                is_supported = False
            elif self.mode.is_control_net and models.arch.supports_edit:
                is_supported = self.mode.can_substitute_instruction(models.arch)
                if not is_supported:
                    self.error_text = _("Not supported for") + f" {models.arch.value}"
            elif self.mode.is_control_net:
                model = models.find_control(self.mode)
                self.has_range = model == models.control.find(self.mode, True)
                if model is None:
                    search_arch = Arch.illu if models.arch is Arch.illu_v else models.arch
                    search_path = (
                        resources.search_path(ResourceKind.controlnet, search_arch, self.mode)
                        or resources.search_path(ResourceKind.model_patch, search_arch, self.mode)
                        or resources.search_path(ResourceKind.lora, models.arch, self.mode)
                    )
                    if search_path:
                        self.error_text = (
                            _("The ControlNet model is not installed") + f" {search_path}"
                        )
                    else:
                        self.error_text = _("Not supported for") + f" {models.arch.value}"
                    is_supported = False

            if not self.mode.is_post_processing and self._index >= client.features.max_control_layers:
                self.error_text = _("Too many control layers")
                is_supported = False

        self.is_supported = is_supported
        self.can_generate = is_supported and self.mode.has_preprocessor

    def _update_is_pose_vector(self):
        self.is_pose_vector = self.mode is ControlMode.pose and self.layer.type is LayerType.vector

    def _update_active_job(self):
        from .jobs import JobState

        active = not (self._generate_job is None or self._generate_job.state is JobState.finished)
        if self.has_active_job and not active:
            self._job = None  # job done
        self.has_active_job = active


class ControlLayerList(QObject):
    """List of control layers for one document."""

    added = pyqtSignal(ControlLayer)
    removed = pyqtSignal(ControlLayer)

    _model: model.Model
    _layers: list[ControlLayer]
    _last_mode = ControlMode.scribble

    def __init__(self, model: model.Model):
        super().__init__()
        self._model = model
        self._layers = []
        self._model.layers.removed.connect(self._remove_layer)

    def add(self):
        layer = self._model.layers.active
        if layer.type.is_filter and layer.parent_layer and not layer.parent_layer.is_root:
            layer = layer.parent_layer
        if not layer.type.is_image:
            layer = next(iter(self._model.layers.images), None)
        if layer is None:  # shouldn't be possible, Krita doesn't allow removing all non-mask layers
            log.warning("Trying to add control layer, but document has no suitable layer")
            return
        mode = ControlMode.reference if self._model.arch.is_edit else self._last_mode
        control = ControlLayer(self._model, mode, layer.id, len(self._layers))
        control.mode_changed.connect(self._update_last_mode)
        self._layers.append(control)
        self.added.emit(control)

    def emplace(self):
        self.add()
        return self[-1]

    def remove(self, control: ControlLayer):
        self._layers.remove(control)
        self.removed.emit(control)

        for i, c in enumerate(self._layers):
            c.index = i

    def to_api(self, bounds: Bounds | None = None, time: int | None = None):
        for layer in (c for c in self._layers if not c.is_supported and c.enabled):
            log.warning(f"Trying to use control layer {layer.mode.name}: {layer.error_text}")
        return [c.to_api(bounds, time) for c in self._layers if c.is_supported and c.enabled]

    def _update_last_mode(self, mode: ControlMode):
        self._last_mode = mode

    def _remove_layer(self, layer: Layer):
        if control := next((c for c in self._layers if c.layer_id == layer.id), None):
            self.remove(control)

    def __len__(self):
        return len(self._layers)

    def __getitem__(self, i):
        return self._layers[i]

    def __iter__(self):
        return iter(self._layers)


class ControlParams(NamedTuple):
    strength: float
    range: tuple[float, float]

    @staticmethod
    def from_dict(data: dict[str, Any]):
        return ControlParams(data["strength"], (data["start"], data["end"]))


class ControlPresets:
    _path: Path
    _user_path: Path
    _presets: dict[str, dict[str, list[dict[str, Any]]]]

    _instance: ControlPresets | None = None

    @classmethod
    def instance(cls) -> ControlPresets:
        if cls._instance is None:
            cls._instance = ControlPresets()
        return cls._instance

    def __init__(self):
        self._path = util.plugin_dir / "presets" / "control.json"
        self._user_path = util.user_data_dir / "presets" / "control.json"
        self._read()

    def get(self, mode: ControlMode, arch: Arch):
        default = self._presets["default"]
        versions = self._presets.get(mode.name, default)
        all = versions.get("all", None)
        presets = versions.get(arch.name, all)
        if presets is None:
            raise PluginError(f"No control strength presets found for {mode} and {arch}")
        return [ControlParams.from_dict(p) for p in presets]

    def interpolate(self, mode: ControlMode, arch: Arch, value: float):
        assert value >= 0 and value <= 1, f"Interpolate value out of range: {value}"
        presets = self.get(mode, arch)
        if len(presets) == 1 or value <= 0:
            return presets[0]
        if value == 1:
            return presets[-1]
        value = value * (len(presets) - 1)
        for i, p0 in enumerate(presets):
            if value < i + 1:
                p1 = presets[i + 1]
                t = value - i
                return ControlParams(
                    _lerp(p0.strength, p1.strength, t),
                    (_lerp(p0.range[0], p1.range[0], t), _lerp(p0.range[1], p1.range[1], t)),
                )
        assert False, f"Interpolation failed: {mode}, {arch}, value={value}, presets={presets}"

    def _read(self):
        self._presets = self._read_file(self._path)
        _validate_presets(self._path, self._presets)
        if self._user_path.exists():
            user = self._read_file(self._user_path)
            if _validate_presets(self._user_path, user):
                _recursive_update(self._presets, user)
        else:
            self._user_path.parent.mkdir(parents=True, exist_ok=True)
            self._user_path.write_text(json.dumps({}, indent=4))

    def _read_file(self, path: Path):
        try:
            return json.load(path.open("r"))
        except Exception as e:
            raise ValueError(f"Failed to read control layer presets file {path}: {e}") from e


def _validate_presets(filepath: Path, data: dict[str, Any]) -> bool:
    control_modes = ["default"] + list(ControlMode.__members__.keys())
    model_archs = list(Arch.__members__.keys())

    for mode, versions in data.items():
        if mode not in control_modes:
            log.error(
                f"Invalid control mode '{mode}' in presets file {filepath}."
                f" Valid modes are: {', '.join(control_modes)}"
            )
            return False
        if not isinstance(versions, dict):
            log.error(f"Invalid presets for mode '{mode}' in presets file {filepath}.")
            return False
        for arch, presets in versions.items():
            if arch not in model_archs:
                log.error(
                    f"Invalid Base model '{arch}' for mode '{mode}' in presets file {filepath}."
                    f" Valid versions are: {', '.join(model_archs)}"
                )
                return False
            if not isinstance(presets, list):
                log.error(
                    f"Invalid presets for '{mode}/{arch}' in presets file {filepath}."
                    f" Expected a list, got {presets}"
                )
                return False
            for p in presets:
                if not isinstance(p, dict) or not all(k in p for k in ("strength", "start", "end")):
                    log.error(
                        f"Invalid preset for '{mode}/{arch}' in presets file {filepath}."
                        f" Expected a {{strength, start, end}}, got {p}"
                    )
                    return False
    return True


control_mode_text = {
    ControlMode.reference: _("Reference"),
    ControlMode.inpaint: _("Inpaint"),
    ControlMode.style: _("Style"),
    ControlMode.composition: _("Composition"),
    ControlMode.face: _("Face"),
    ControlMode.universal: _("Universal"),
    ControlMode.color_match: _("Color Match"),
    ControlMode.light_map: _("Light Map"),
    ControlMode.scribble: _("Scribble"),
    ControlMode.line_art: _("Line Art"),
    ControlMode.soft_edge: _("Soft Edge"),
    ControlMode.canny_edge: _("Canny Edge"),
    ControlMode.depth: _("Depth"),
    ControlMode.normal: _("Normal"),
    ControlMode.pose: _("Pose"),
    ControlMode.segmentation: _("Segment"),
    ControlMode.blur: _("Unblur"),
    ControlMode.stencil: _("Stencil"),
    ControlMode.hands: _("Hands"),
}


control_mode_tooltips = {
    ControlMode.reference: _(
        "Uses the layer as a visual reference through IP-Adapter or Redux.\n"
        "Affects: subject identity, colors, materials, and the overall look.\n"
        "Best when the result should resemble the image without locking every edge."
    ),
    ControlMode.style: _(
        "Transfers the visual style from the layer through IP-Adapter.\n"
        "Affects: color palette, lighting mood, medium, texture, and rendering style.\n"
        "Best when you want the prompt composition but the reference image's look."
    ),
    ControlMode.composition: _(
        "Uses the layer as a composition reference through IP-Adapter.\n"
        "Affects: layout, framing, camera angle, object placement, and broad shapes.\n"
        "Best when pose and arrangement matter more than exact texture or identity."
    ),
    ControlMode.face: _(
        "Uses face ID guidance from the layer through IP-Adapter FaceID.\n"
        "Affects: facial identity and key facial features.\n"
        "Best with a clear face; it does not strongly control clothing, pose, or background."
    ),
    ControlMode.inpaint: _(
        "Internal inpaint control for masked edits.\n"
        "Affects: masked regions and their connection to the surrounding image.\n"
        "Normally created by the edit workflow rather than selected manually."
    ),
    ControlMode.universal: _(
        "Internal union ControlNet mode used when a universal control model is installed.\n"
        "Affects: the same structure as the selected visible control type.\n"
        "Normally selected automatically by the workflow."
    ),
    ControlMode.color_match: _(
        "Matches the generated image colors to this layer after sampling.\n"
        "Affects: color balance, saturation, and overall palette without changing structure.\n"
        "Best for preventing flat or dull results while preserving the generated details."
    ),
    ControlMode.light_map: _(
        "Screen-blends bright areas from this layer after sampling.\n"
        "Affects: highlights, glow, colored light, and broad light placement without changing structure.\n"
        "Best with a dark layer containing painted or preserved highlight areas."
    ),
    ControlMode.scribble: _(
        "Uses a rough sketch or generated scribble map as structure guidance.\n"
        "Affects: major contours, placement, and simple shapes.\n"
        "Best for loose drawings where the model should invent the final details."
    ),
    ControlMode.line_art: _(
        "Uses clean line art as structure guidance.\n"
        "Affects: outlines, silhouettes, object boundaries, and shape placement.\n"
        "Best for coloring or realistic translation of line drawings."
    ),
    ControlMode.soft_edge: _(
        "Uses soft edge detection as structure guidance.\n"
        "Affects: object boundaries and visible forms with more flexibility than Canny.\n"
        "Best when you want structure without forcing every high-contrast edge."
    ),
    ControlMode.canny_edge: _(
        "Uses a hard Canny edge map as structure guidance.\n"
        "Affects: sharp edges, silhouettes, composition, and fine boundary placement.\n"
        "Best for strict shape matching; high strength can over-constrain the result."
    ),
    ControlMode.depth: _(
        "Uses a depth map as structure guidance.\n"
        "Affects: foreground/background separation, camera perspective, and 3D layout.\n"
        "Best for keeping scene geometry while allowing colors and details to change."
    ),
    ControlMode.normal: _(
        "Uses a surface normal map as structure guidance.\n"
        "Affects: surface direction, form, volume, and lighting-facing geometry.\n"
        "Best for shaded or 3D-like sources where object form matters."
    ),
    ControlMode.pose: _(
        "Uses an OpenPose skeleton as body guidance.\n"
        "Affects: body pose, hands, face keypoints, and character placement.\n"
        "Best for matching a character pose without copying clothing or identity."
    ),
    ControlMode.segmentation: _(
        "Uses semantic regions as layout guidance.\n"
        "Affects: where broad object categories and scene regions appear.\n"
        "Best for controlling scene organization without preserving fine edges."
    ),
    ControlMode.blur: _(
        "Uses tile/unblur control to preserve local image content while adding detail.\n"
        "Affects: texture, colors, small structures, and restoration/upscale consistency.\n"
        "Best for upscaling, sharpening, or regenerating detail from a blurry source."
    ),
    ControlMode.stencil: _(
        "Uses a high-contrast stencil or pattern as strong structure guidance.\n"
        "Affects: silhouettes, graphic shapes, readable patterns, and overall composition.\n"
        "Best for QR/stencil-like layouts where the shape must survive generation."
    ),
    ControlMode.hands: _(
        "Uses a hand-focused depth preprocessor as structure guidance.\n"
        "Affects: hand shape, finger placement, and hand depth.\n"
        "Best for hand correction or preserving hand placement from the source."
    ),
}


def control_mode_tooltip(mode: ControlMode):
    return control_mode_tooltips.get(mode, mode.text)


def _lerp(a: float, b: float, t: float) -> float:
    return a + t * (b - a)


def _recursive_update(a: dict[str, Any], b: dict[str, Any]):
    for k, v in b.items():
        if isinstance(v, dict):
            a[k] = _recursive_update(a.get(k, {}), v)
        else:
            a[k] = v
    return a
