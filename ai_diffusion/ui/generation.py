from __future__ import annotations

from html import escape
from textwrap import wrap as wrap_text
from typing import ClassVar, cast

from PyQt5.QtCore import (
    QEvent,
    QItemSelectionModel,
    QMetaObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    QUuid,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPalette,
    QPainter,
)
from PyQt5.QtWidgets import (
    QAction,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..image import Bounds, Extent, Image
from ..jobs import Job, JobKind, JobParams, JobQueue, JobState
from ..localization import translate as _
from ..model import InpaintContext, Model, ProgressKind, RootRegion, Workspace
from ..properties import Bind, Binding, bind, bind_combo, bind_toggle
from ..resources import Arch
from ..root import root
from ..settings import settings
from ..style import Styles
from ..util import ensure, flatten, sequence_equal
from ..workflow import FillMode, InpaintMode
from . import actions, theme
from .region import RegionPromptWidget
from .widget import (
    ErrorBox,
    GenerateButton,
    LayerCountWidget,
    LoraDockerPanel,
    QuickStyleBar,
    QueueButton,
    StrengthWidget,
    StyleParamsWidget,
    StyleSelectWidget,
    VramWidget,
    WorkspaceSelectWidget,
    create_wide_tool_button,
)


class ImageCompareLabel(QLabel):
    def __init__(self, left: Image, right: Image, parent=None):
        super().__init__(parent)
        self._left = left
        self._right = right
        self._position = 50
        self.setMinimumSize(420, 320)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_position(self, value: int):
        self._position = value
        self.update()

    def paintEvent(self, event: QPaintEvent | None):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())

        target = self._target_rect()
        if target.isEmpty():
            return

        right = self._right.to_pixmap().scaled(
            target.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        left = self._left.to_pixmap().scaled(
            target.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        image_rect = QRect(
            target.x() + (target.width() - right.width()) // 2,
            target.y() + (target.height() - right.height()) // 2,
            right.width(),
            right.height(),
        )

        painter.drawPixmap(image_rect, right)
        split = image_rect.x() + round(image_rect.width() * self._position / 100)
        painter.save()
        painter.setClipRect(
            QRect(image_rect.x(), image_rect.y(), split - image_rect.x(), image_rect.height())
        )
        painter.drawPixmap(image_rect, left)
        painter.restore()
        painter.setPen(QColor("#ffd43b"))
        painter.drawLine(split, image_rect.y(), split, image_rect.bottom())

    def _target_rect(self):
        return self.rect().adjusted(6, 6, -6, -6)


class ImageCompareDialog(QDialog):
    def __init__(self, left: Image, right: Image, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Compare Images"))
        layout = QVBoxLayout(self)
        self._label = ImageCompareLabel(left, right, self)
        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setRange(0, 100)
        self._slider.setValue(50)
        self._slider.valueChanged.connect(self._label.set_position)
        layout.addWidget(self._label)
        layout.addWidget(self._slider)
        self._resize_to_image(left, right)

    def _resize_to_image(self, left: Image, right: Image):
        image_width = max(left.width, right.width)
        image_height = max(left.height, right.height)
        screen = QGuiApplication.screenAt(self.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(max(420, image_width), max(320, image_height))
            return

        available = screen.availableGeometry()
        chrome = QSize(36, 96)
        max_size = available.size() - QSize(40, 60)
        target = QSize(image_width, image_height) + chrome
        target.setWidth(max(420, min(target.width(), max_size.width())))
        target.setHeight(max(320, min(target.height(), max_size.height())))
        self.resize(target)


class HistoryWidget(QListWidget):
    _model: Model
    _connections: list[QMetaObject.Connection]
    _settings_connection: QMetaObject.Connection
    _last_group_key: tuple | None = None

    item_activated = pyqtSignal(QListWidgetItem)

    _thumb_size = 96
    _applied_icon = Image.load(theme.icon_path / "star.png")
    _list_css = f"""
        QListWidget {{ background-color: transparent; }}
        QListWidget::item:selected {{ border: 1px solid {theme.grey}; }}
    """
    _button_css = f"""
        QPushButton {{
            border: 1px solid {theme.grey};
            background: {"rgba(64, 64, 64, 170)" if theme.is_dark else "rgba(240, 240, 240, 160)"};
            padding: 2px;
        }}
        QPushButton:hover {{
            background: {"rgba(72, 72, 72, 210)" if theme.is_dark else "rgba(240, 240, 240, 200)"};
        }}
    """

    def __init__(self, parent: QWidget | None):
        super().__init__(parent)
        self._model = root.active_model
        self._connections = []

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setResizeMode(QListView.Adjust)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFlow(QListView.LeftToRight)
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(theme.screen_scale(self, QSize(self._thumb_size, self._thumb_size)))
        self.setFrameStyle(QListWidget.NoFrame)
        self.setStyleSheet(self._list_css)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setDragEnabled(False)
        self.itemClicked.connect(self.handle_preview_click)
        self.itemDoubleClicked.connect(self.item_activated)
        self.itemSelectionChanged.connect(self.select_item)

        self._apply_button = QPushButton(theme.icon("apply"), _("Apply"), self)
        self._apply_button.setStyleSheet(self._button_css)
        self._apply_button.setVisible(False)
        self._apply_button.clicked.connect(self._activate_selection)

        self._context_button = QPushButton(theme.icon("context"), "", self)
        self._context_button.setStyleSheet(self._button_css)
        self._context_button.setVisible(False)
        self._context_button.clicked.connect(self._show_context_menu_dropdown)

        f = self.fontMetrics()
        self._apply_button.setFixedHeight(f.height() + 8)
        self._context_button.setFixedWidth(f.height() + 8)
        if scrollbar := self.verticalScrollBar():
            scrollbar.valueChanged.connect(self.update_apply_button)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self._settings_connection = settings.changed.connect(self._handle_settings_changed)

    @property
    def model_(self):
        return self._model

    @model_.setter
    def model_(self, model: Model):
        Binding.disconnect_all(self._connections)
        self._model = model
        jobs = model.jobs
        self._connections = [
            jobs.selection_changed.connect(self.update_selection),
            jobs.job_finished.connect(self.add),
            jobs.job_discarded.connect(self.remove),
            jobs.result_used.connect(self.update_image_thumbnail),
            jobs.result_discarded.connect(self.remove_image),
        ]
        self.rebuild()
        self.update_selection()

    def add(self, job: Job, is_new: bool = True):
        if not self.is_finished(job):
            return  # Only finished diffusion/animation jobs have images to show

        scrollbar = self.verticalScrollBar()
        scroll_to_bottom = scrollbar and scrollbar.value() >= scrollbar.maximum() - 4

        group_key = self._history_group_key(job.params)
        if self._last_group_key != group_key:
            self._last_group_key = group_key
            prompt = job.params.name if job.params.name != "" else "<no prompt>"
            strength = job.params.metadata.get("strength", 1.0)
            strength = f"{strength * 100:.0f}% - " if strength != 1.0 else ""

            header = QListWidgetItem(f"{job.timestamp:%H:%M} - {strength}{prompt}")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setData(Qt.ItemDataRole.UserRole, job.id)
            header.setData(Qt.ItemDataRole.ToolTipRole, job.params.prompt)
            header.setSizeHint(QSize(9999, self.fontMetrics().lineSpacing() + 4))
            header.setTextAlignment(Qt.AlignmentFlag.AlignLeft)
            self.addItem(header)

        if job.kind is JobKind.diffusion:
            if job.params.is_layered:
                self._add_item(job, QListWidgetItem(self._image_thumbnail(job, 0, is_new), None))
            else:
                for i, img in enumerate(job.results):
                    self._add_item(
                        job,
                        QListWidgetItem(self._image_thumbnail(job, i, is_new), None),
                        i,
                        is_new,
                    )

        if job.kind is JobKind.animation:
            item = AnimatedListItem([
                self._image_thumbnail(job, i, is_new) for i in range(len(job.results))
            ])
            self._add_item(job, item, is_new=is_new)

        if scroll_to_bottom:
            self.scrollToBottom()

    def _add_item(self, job: Job, item: QListWidgetItem, index=0, is_new: bool = True):
        item.setData(Qt.ItemDataRole.UserRole, job.id)
        item.setData(Qt.ItemDataRole.UserRole + 1, index)
        item.setData(Qt.ItemDataRole.UserRole + 2, is_new)
        item.setData(Qt.ItemDataRole.ToolTipRole, self._job_info_html(job.params))
        self.addItem(item)

    def _history_group_key(self, params: JobParams):
        meta = params.metadata
        return (
            meta.get("prompt_final") or meta.get("prompt_eval") or meta.get("prompt") or params.name,
            meta.get("negative_prompt_final")
            or meta.get("negative_prompt_eval")
            or meta.get("negative_prompt", ""),
            meta.get("style", ""),
            meta.get("checkpoint", ""),
            repr(meta.get("loras", [])),
            repr(meta.get("control", [])),
        )

    _job_info_translations: ClassVar[dict[str, str]] = {
        "prompt": _("Prompt"),
        "prompt_eval": _("Prompt (Evaluated)"),
        "prompt_final": _("Prompt (Final)"),
        "negative_prompt": _("Negative Prompt"),
        "negative_prompt_eval": _("Negative Prompt (Evaluated)"),
        "negative_prompt_final": _("Negative Prompt (Final)"),
        "style": _("Style"),
        "strength": _("Strength"),
        "denoise": _("Denoise"),
        "checkpoint": _("Model"),
        "loras": _("LoRA"),
        "sampler": _("Sampler"),
        "seed": _("Seed"),
        "steps": _("Sampler Steps"),
        "actual_steps": _("Actual Steps"),
        "total_steps": _("Total Steps"),
        "guidance": _("Guidance Strength (CFG Scale)"),
        "control": _("Control Layers"),
        "text_encoders": _("Text Encoders"),
    }
    _prompt_section_settings: ClassVar[tuple[tuple[str, str, str], ...]] = (
        ("history_show_prompt", "prompt", "negative_prompt"),
        ("history_show_prompt_evaluated", "prompt_eval", "negative_prompt_eval"),
        ("history_show_prompt_final", "prompt_final", "negative_prompt_final"),
    )

    def _job_title(self, params: JobParams):
        title = params.name if params.name != "" else "<no prompt>"
        if len(title) > 70:
            title = title[:66] + "..."
        if params.strength != 1.0:
            title = f"{title} @ {params.strength * 100:.0f}%"
        return title

    def _style_name(self, params: JobParams):
        style = Styles.list().find(params.style)
        if style:
            return f"{style.name} ({style.filename})"
        return params.style

    def _short_file(self, value: object):
        text = str(value).replace("\\", "/")
        return text.rsplit("/", 1)[-1] if "/" in text else text

    def _format_float(self, value: object):
        if isinstance(value, float):
            return f"{value:.2f}".rstrip("0").rstrip(".")
        return str(value)

    def _format_loras(self, value: object):
        if not isinstance(value, list):
            return [str(value)]
        result = []
        for lora in value:
            if not isinstance(lora, dict) or not lora.get("enabled", True):
                continue
            weight = lora.get("weight", lora.get("strength", "?"))
            result.append(f"{self._short_file(lora.get('name', ''))}  {self._format_float(weight)}")
        return result

    def _format_control(self, value: object):
        if not isinstance(value, list):
            return [str(value)]
        result = []
        for control in value:
            if not isinstance(control, dict):
                continue
            text = f"{control.get('mode')}: {control.get('image', '')}"
            text += f"  @{self._format_float(control.get('strength', '?'))}"
            start = control.get("start")
            end = control.get("end")
            if start is not None and end is not None:
                text += f"  {self._format_float(start)}-{self._format_float(end)}"
            result.append(text)
        return result

    def _format_text_encoders(self, value: object):
        if not isinstance(value, dict):
            return [str(value)]
        return [f"{key}: {self._short_file(model)}" for key, model in value.items() if model]

    def _prompt_value(self, params: JobParams, key: str):
        value = params.metadata.get(key, "")
        return value if isinstance(value, str) else str(value)

    def _dedup_prompt_sections(self, params: JobParams):
        enabled = [
            item for item in self._prompt_section_settings if getattr(settings, item[0])
        ]
        keys = [positive for _, positive, _ in enabled]
        keys.extend(negative for _, _, negative in enabled)
        seen: set[str] = set()
        for key in keys:
            value = self._prompt_value(params, key).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            yield self._job_info_translations[key], value

    def _handle_settings_changed(self, key: str, _value: object):
        if key in {setting for setting, _, _ in self._prompt_section_settings}:
            self._refresh_tooltips()

    def _refresh_tooltips(self):
        for i in range(self.count()):
            item = self.item(i)
            if item is None or item.data(Qt.ItemDataRole.UserRole + 1) is None:
                continue
            job_id = item.data(Qt.ItemDataRole.UserRole)
            if job := self._model.jobs.find(job_id):
                item.setData(Qt.ItemDataRole.ToolTipRole, self._job_info_html(job.params))

    def _core_info(self, params: JobParams):
        meta = params.metadata
        rows = [
            (_("Style"), self._style_name(params)),
            (_("Model"), meta.get("checkpoint", "")),
            (_("Sampler"), meta.get("sampler", "")),
            (_("Steps"), meta.get("steps", "")),
            (_("Guidance"), meta.get("guidance", "")),
        ]
        if scheduled := meta.get("scheduled_cfg"):
            if isinstance(scheduled, dict):
                start = self._format_float(scheduled.get("from", ""))
                end = self._format_float(scheduled.get("to", ""))
                schedule = scheduled.get("schedule", "")
                rows.append((_("Scheduled CFG"), f"{start} -> {end} {schedule}".strip()))
        if "denoise" in meta:
            denoise = f"{float(meta['denoise']) * 100:.0f}%"
            actual = meta.get("actual_steps")
            total = meta.get("total_steps")
            if actual and total:
                denoise += f" ({actual}/{total})"
            rows.append((_("Denoise"), denoise))
        elif params.strength != 1.0:
            rows.append((_("Denoise"), f"{params.strength * 100:.0f}%"))
        rows.append((_("Seed"), params.seed))
        return [(label, value) for label, value in rows if value not in ("", None, [])]

    def _wrap_plain(self, value: str, width=92):
        lines: list[str] = []
        for paragraph in value.splitlines() or [""]:
            lines.extend(wrap_text(paragraph, width=width) or [""])
        return lines

    def _wrap_html(self, value: str, width=92):
        return "<br/>".join(escape(line) for line in self._wrap_plain(value, width))

    def _job_info_plain(self, params: JobParams, include_usage=True):
        strings: list[str] = [self._job_title(params)]
        if include_usage:
            strings.extend(["", _("Click to toggle preview, double-click to apply.")])
        strings.append("")

        for label, value in self._core_info(params):
            strings.append(f"{label}: {value}")

        for key, formatter in [
            ("loras", self._format_loras),
            ("control", self._format_control),
            ("text_encoders", self._format_text_encoders),
        ]:
            lines = formatter(params.metadata.get(key, []))
            if lines:
                strings.append("")
                strings.append(self._job_info_translations[key] + ":")
                strings.extend(f"  {line}" for line in lines)

        for label, value in self._dedup_prompt_sections(params):
            strings.append("")
            strings.append(label + ":")
            strings.extend(f"  {line}" for line in self._wrap_plain(value))

        return "\n".join(flatten(strings))

    def _job_info_html(self, params: JobParams):
        title = escape(self._job_title(params))
        usage = escape(_("Click to toggle preview, double-click to apply."))
        parts = [
            "<qt><div style='width: 560px;'>",
            f"<b>{title}</b><br/>",
            f"<span style='color: {theme.grey};'>{usage}</span>",
            "<hr/>",
            "<table cellspacing='2' cellpadding='0'>",
        ]
        for label, value in self._core_info(params):
            parts.append(
                "<tr>"
                f"<td><b>{escape(str(label))}</b></td>"
                f"<td>{escape(str(value))}</td>"
                "</tr>"
            )
        parts.append("</table>")

        for key, formatter in [
            ("loras", self._format_loras),
            ("control", self._format_control),
            ("text_encoders", self._format_text_encoders),
        ]:
            lines = formatter(params.metadata.get(key, []))
            if lines:
                label = escape(self._job_info_translations[key])
                body = "<br/>".join(escape(line) for line in lines)
                parts.append(f"<p><b>{label}</b><br/>{body}</p>")

        for label, value in self._dedup_prompt_sections(params):
            parts.append(
                f"<p><b>{escape(label)}</b><br/>"
                f"<span style='font-family: monospace;'>{self._wrap_html(value)}</span></p>"
            )

        parts.append("</div></qt>")
        return "".join(parts)

    def remove(self, job: Job):
        self._remove_items(ensure(job.id))

    def remove_image(self, id: JobQueue.Item):
        self._remove_items(id.job, id.image)

    def _remove_items(self, job_id: str, image_index: int = -1):
        def _job_id(item: QListWidgetItem | None):
            return item.data(Qt.ItemDataRole.UserRole) if item else None

        item_was_selected = False
        with theme.SignalBlocker(self):
            # Remove all the job's items before triggering potential selection changes
            current = next((i for i in range(self.count()) if _job_id(self.item(i)) == job_id), -1)
            if current >= 0:
                item = self.item(current)
                while item and _job_id(item) == job_id:
                    _, index = self.item_info(item)
                    if image_index == index or (index is not None and image_index == -1):
                        item_was_selected = item_was_selected or item.isSelected()
                        self.takeItem(current)
                    else:
                        if index and index > image_index:
                            item.setData(Qt.ItemDataRole.UserRole + 1, index - 1)
                        current += 1
                    item = self.item(current)

        if item_was_selected:
            self._model.jobs.selection = []
        else:
            self.update_apply_button()  # selection may have moved

        for i in range(self.count()):
            item = self.item(i)
            next_item = self.item(i + 1)
            if item and item.text() != "" and next_item and next_item.text() != "":
                self.takeItem(i)

    def update_selection(self):
        current = [self._item_data(i) for i in self.selectedItems()]
        changed = not sequence_equal(self._model.jobs.selection, current)

        with theme.SignalBlocker(self):
            for i in range(self.count()):
                item = self.item(i)
                if item and item.type() == QListWidgetItem.ItemType.UserType:
                    cast(AnimatedListItem, item).stop_animation()

            if changed:  # don't mess with widget's state if it already matches
                self.clearSelection()

            for selection in self._model.jobs.selection:
                if item := self._find(selection):
                    if changed:
                        item.setSelected(True)
                    if item.type() == QListWidgetItem.ItemType.UserType:
                        cast(AnimatedListItem, item).start_animation()

        self.update_apply_button()

    def update_apply_button(self):
        selected = self.selectedItems()
        if len(selected) > 0:
            rect = self.visualItemRect(selected[0])
            font = self._apply_button.fontMetrics()
            context_visible = rect.width() >= 0.6 * self.iconSize().width()
            apply_text_visible = font.width(_("Apply")) < 0.35 * rect.width()
            apply_pos = QPoint(rect.left() + 3, rect.bottom() - self._apply_button.height() - 2)
            if context_visible:
                cw = self._context_button.width()
                context_pos = QPoint(rect.right() - cw - 2, apply_pos.y())
                context_size = QSize(cw, self._apply_button.height())
            else:
                context_pos = QPoint(rect.right(), apply_pos.y())
                context_size = QSize(0, 0)
            apply_size = QSize(context_pos.x() - rect.left() - 5, self._apply_button.height())
            self._apply_button.setVisible(True)
            self._apply_button.move(apply_pos)
            self._apply_button.resize(apply_size)
            self._apply_button.setText(_("Apply") if apply_text_visible else "")
            self._context_button.setVisible(context_visible)
            if context_visible:
                self._context_button.move(context_pos)
                self._context_button.resize(context_size)
        else:
            self._apply_button.setVisible(False)
            self._context_button.setVisible(False)

    def update_image_thumbnail(self, id: JobQueue.Item):
        if item := self._find(id):
            job = ensure(self._model.jobs.find(id.job))
            item.setIcon(self._image_thumbnail(job, id.image, self._is_new(item)))

    def select_item(self):
        for item in self.selectedItems():
            self._clear_new_flag(item)
        self._model.jobs.selection = [self._item_data(i) for i in self.selectedItems()]

    def _toggle_selection(self):
        self._model.jobs.toggle_selection()

    def _activate_selection(self):
        items = self.selectedItems()
        if len(items) > 0:
            self.item_activated.emit(items[0])

    def is_finished(self, job: Job):
        return job.kind in [JobKind.diffusion, JobKind.animation] and job.state is JobState.finished

    def rebuild(self):
        self.clear()
        self._last_group_key = None
        for job in filter(self.is_finished, self._model.jobs):
            self.add(job, is_new=False)
        self.scrollToBottom()

    def item_info(self, item: QListWidgetItem) -> tuple[str, int]:  # job id, image index
        return item.data(Qt.ItemDataRole.UserRole), item.data(Qt.ItemDataRole.UserRole + 1)

    @property
    def selected_job(self) -> Job | None:
        items = self.selectedItems()
        if len(items) > 0:
            job_id, _ = self.item_info(items[0])
            return self._model.jobs.find(job_id)
        return None

    def handle_preview_click(self, item: QListWidgetItem):
        if item.text() != "" and item.text() != "<no prompt>":
            if clipboard := QGuiApplication.clipboard():
                prompt = item.data(Qt.ItemDataRole.ToolTipRole)
                clipboard.setText(prompt)

    def mousePressEvent(self, e: QMouseEvent | None):
        if (  # make single click deselect current item (usually requires Ctrl+click)
            e is not None
            and e.button() == Qt.MouseButton.LeftButton
            and e.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            item = self.itemAt(e.pos())
            if item is not None and item.isSelected():
                self.clearSelection()
                e.accept()
                return
        super().mousePressEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update_apply_button()

    def event(self, e: QEvent | None):
        assert e is not None
        # Disambiguate shortcut events which Krita overrides
        if e.type() == QEvent.Type.ShortcutOverride:
            assert isinstance(e, QKeyEvent)
            if e.matches(QKeySequence.StandardKey.Delete):
                self._discard_image(confirm=False)
                e.accept()
            elif e.key() == Qt.Key.Key_Space:
                self._toggle_selection()
                e.accept()
        return super().event(e)

    def _find(self, id: JobQueue.Item):
        items = (ensure(self.item(i)) for i in range(self.count()))
        return next((item for item in items if self._item_data(item) == id), None)

    def _item_data(self, item: QListWidgetItem):
        return JobQueue.Item(
            item.data(Qt.ItemDataRole.UserRole), item.data(Qt.ItemDataRole.UserRole + 1)
        )

    def _is_new(self, item: QListWidgetItem):
        return bool(item.data(Qt.ItemDataRole.UserRole + 2))

    def _clear_new_flag(self, item: QListWidgetItem):
        if not self._is_new(item):
            return
        item.setData(Qt.ItemDataRole.UserRole + 2, False)
        job_id, index = self.item_info(item)
        if job := self._model.jobs.find(job_id):
            item.setIcon(self._image_thumbnail(job, index, False))

    def _image_thumbnail(self, job: Job, index: int, is_new: bool = False):
        image = job.results[index]
        # Use 2x thumb size for good quality on high-DPI screens
        thumb = Image.scale_to_fit(image, Extent(self._thumb_size * 2, self._thumb_size * 2))
        min_height = min(4 * self._apply_button.height(), 2 * self._thumb_size)
        if thumb.extent.height < min_height:
            thumb = Image.crop(thumb, Bounds(0, 0, thumb.extent.width, min_height))
        if job.result_was_used(index):  # add tiny star icon to mark used results
            thumb.draw_image(self._applied_icon, offset=(thumb.extent.width - 28, 4))
        pixmap = thumb.to_pixmap()
        if is_new:
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            text = _("NEW")
            font = painter.font()
            font.setBold(True)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            padding = 5
            rect = QRect(
                4,
                4,
                metrics.horizontalAdvance(text) + padding * 2,
                metrics.height() + 4,
            )
            painter.setPen(QColor(20, 20, 20, 220))
            painter.setBrush(QColor("#ffd43b"))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QColor("#111111"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
            painter.end()
        return QIcon(pixmap)

    def _show_context_menu(self, pos: QPoint):
        item = self.itemAt(pos)
        if item is not None:
            job = self._model.jobs.find(self._item_data(item).job)
            menu = QMenu(self)
            menu.addAction(_("Copy Prompt"), self._copy_prompt)
            menu.addAction(_("Copy Prompt (Evaluated)"), self._copy_prompt_evaluated)
            menu.addAction(_("Copy Strength"), self._copy_strength)
            style_action = ensure(menu.addAction(_("Copy Style"), self._copy_style))
            if job is None or Styles.list().find(job.params.style) is None:
                style_action.setEnabled(False)
            menu.addAction(_("Copy Seed"), self._copy_seed)
            menu.addAction(_("Info to Clipboard"), self._info_to_clipboard)
            menu.addSeparator()
            compare_action = ensure(menu.addAction(_("Compare Selected"), self._compare_selected))
            compare_action.setEnabled(len(self._selected_images()) == 2)
            save_action = ensure(menu.addAction(_("Save Image"), self._save_image))
            if self._model.document.filename == "":
                tt = _(
                    "Save as separate image to the same folder as the document.\nMust save the document first!"
                )
                save_action.setEnabled(False)
                save_action.setToolTip(tt)
                menu.setToolTipsVisible(True)
            menu.addAction(_("Discard Image"), self._discard_image)
            menu.addSeparator()
            menu.addAction(_("Clear History"), self._clear_all)
            menu.exec(self.mapToGlobal(pos))

    def _show_context_menu_dropdown(self):
        pos = self._context_button.pos()
        pos.setY(pos.y() + self._context_button.height())
        self._show_context_menu(pos)

    def _copy_prompt(self, evaluated=False):
        if job := self.selected_job:
            positive = "prompt_eval" if evaluated else "prompt"
            prompt = job.params.metadata.get(positive, job.params.prompt)
            active = self._model.active_regions.active_or_root
            active.positive = prompt
            if isinstance(active, RootRegion):
                negative = "negative_prompt_eval" if evaluated else "negative_prompt"
                active.negative = job.params.metadata.get(
                    negative, job.params.metadata.get("negative_prompt", "")
                )

            if clipboard := QGuiApplication.clipboard():
                clipboard.setText(prompt)

            if self._model.workspace is Workspace.custom and self._model.document.is_active:
                self._model.custom.try_set_params(job.params.metadata)

    def _copy_prompt_evaluated(self):
        self._copy_prompt(evaluated=True)

    def _copy_strength(self):
        if job := self.selected_job:
            self._model.strength = job.params.strength

    def _copy_style(self):
        if (job := self.selected_job) and (style := Styles.list().find(job.params.style)):
            self._model.style = style

    def _copy_seed(self):
        if job := self.selected_job:
            self._model.fixed_seed = True
            self._model.seed = job.params.seed

    def _info_to_clipboard(self):
        if (job := self.selected_job) and (clipboard := QGuiApplication.clipboard()):
            clipboard.setText(self._job_info_plain(job.params, include_usage=False))

    def _selected_images(self):
        images = []
        for item in self.selectedItems():
            data = self._item_data(item)
            if job := self._model.jobs.find(data.job):
                if isinstance(data.image, int) and 0 <= data.image < len(job.results):
                    images.append(job.results[data.image])
        return images

    def _compare_selected(self):
        images = self._selected_images()
        if len(images) == 2:
            dialog = ImageCompareDialog(images[0], images[1], self)
            dialog.exec()

    def _save_image(self):
        items = self.selectedItems()
        for item in items:
            job_id, image_index = self.item_info(item)
            self._model.save_result(job_id, image_index)

    def _discard_image(self, confirm=True):
        confirm = confirm and settings.confirm_discard_image
        reply = QMessageBox.Yes
        if confirm:
            reply = QMessageBox.warning(
                self,
                _("Discard Image"),
                _("Are you sure you want to discard the selected images?"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.StandardButton.Yes,
            )
        if reply == QMessageBox.Yes:
            items = self.selectedItems()
            next_item = self.row(items[0]) if len(items) > 0 else -1
            for item in items:
                job_id, image_index = self.item_info(item)
                self._model.jobs.discard(job_id, image_index)
            if next_item >= 0:
                self.setCurrentRow(next_item, QItemSelectionModel.SelectionFlag.Current)

    def _clear_all(self):
        reply = QMessageBox.warning(
            self,
            _("Clear History"),
            _("Are you sure you want to discard all generated images?"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._model.jobs.clear()
            self.clear()
            self._last_group_key = None
            self._model.hide_preview(delete_layer=True)


class AnimatedListItem(QListWidgetItem):
    def __init__(self, images: list[QIcon]):
        super().__init__(images[0], None, type=QListWidgetItem.ItemType.UserType)
        self._images = images
        self._current = 0
        self._is_running = False
        self._timer = QTimer()
        self._timer.setSingleShot(False)
        self._timer.timeout.connect(self._next_frame)

    def start_animation(self):
        if not self._is_running:
            self._is_running = True
            self._timer.start(40)

    def stop_animation(self):
        if self._is_running:
            self._timer.stop()
            self._is_running = False
            self._current = 0
            self.setIcon(self._images[self._current])

    def _next_frame(self):
        self._current = (self._current + 1) % len(self._images)
        self.setIcon(self._images[self._current])


class CustomInpaintWidget(QWidget):
    _model: Model
    _model_bindings: list[QMetaObject.Connection | Binding]

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._model = root.active_model
        self._model_bindings = []

        self.use_inpaint_button = QCheckBox(self)
        self.use_inpaint_button.setText(_("Seamless"))
        self.use_inpaint_button.setToolTip(_("Generate content which blends into the surroundings"))

        self.use_prompt_focus_button = QCheckBox(self)
        self.use_prompt_focus_button.setText(_("Focus"))
        self.use_prompt_focus_button.setToolTip(
            _(
                "Use the text prompt to describe the selected region rather than the context area / Use only one regional prompt"
            )
        )

        self.edit_mode_switch = QCheckBox(self)
        self.edit_mode_switch.setText(_("Edit"))
        self.edit_mode_switch.setToolTip(_("Edit canvas with text instructions"))

        self.fill_mode_combo = QComboBox(self)
        fill_icon = theme.icon("fill")
        self.fill_mode_combo.addItem(theme.icon("fill-empty"), _("None"), FillMode.none)
        self.fill_mode_combo.addItem(fill_icon, _("Neutral"), FillMode.neutral)
        self.fill_mode_combo.addItem(fill_icon, _("Blur"), FillMode.blur)
        self.fill_mode_combo.addItem(fill_icon, _("Border"), FillMode.border)
        self.fill_mode_combo.addItem(fill_icon, _("Inpaint"), FillMode.inpaint)
        self.fill_mode_combo.setStyleSheet(theme.flat_combo_stylesheet)
        self.fill_mode_combo.setToolTip(_("Pre-fill the selected region before diffusion"))

        def ctx_icon(name):
            return theme.icon(f"context-{name}")

        self.context_combo = QComboBox(self)
        self.context_combo.addItem(
            ctx_icon("automatic"), _("Automatic Context"), InpaintContext.automatic
        )
        self.context_combo.addItem(
            ctx_icon("mask"), _("Selection Bounds"), InpaintContext.mask_bounds
        )
        self.context_combo.addItem(
            ctx_icon("image"), _("Entire Image"), InpaintContext.entire_image
        )
        self.context_combo.setStyleSheet(theme.flat_combo_stylesheet)
        self.context_combo.setToolTip(
            _("Part of the image around the selection which is used as context.")
        )
        self.context_combo.setMinimumContentsLength(20)
        self.context_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLength
        )
        self.context_combo.currentIndexChanged.connect(self.set_context)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.use_inpaint_button)
        layout.addWidget(self.use_prompt_focus_button)
        layout.addWidget(self.edit_mode_switch)
        layout.addWidget(self.fill_mode_combo, 1)
        layout.addWidget(self.context_combo, 1)
        self.setLayout(layout)

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        if self._model != model:
            Binding.disconnect_all(self._model_bindings)
            self._model = model
            self._model_bindings = [
                bind_combo(model.inpaint, "fill", self.fill_mode_combo),
                bind_toggle(model.inpaint, "use_inpaint", self.use_inpaint_button),
                bind_toggle(model.inpaint, "use_prompt_focus", self.use_prompt_focus_button),
                bind_toggle(model, "edit_mode", self.edit_mode_switch),
                model.style_changed.connect(self.update_widgets_enabled),
                model.strength_changed.connect(self.update_widgets_enabled),
                model.layers.changed.connect(self.update_context_layers),
                model.edit_mode_changed.connect(self.update_widgets_enabled),
            ]
            self.update_widgets_enabled()
            self.update_context_layers()
            self.update_context()

    def update_widgets_enabled(self):
        arch = self._model.arch
        self.fill_mode_combo.setEnabled(self.model.strength == 1.0 and not self.model.is_editing)
        self.use_inpaint_button.setEnabled(arch.is_sdxl_like or arch.has_controlnet_inpaint)
        self.use_prompt_focus_button.setVisible(arch is Arch.sd15 or arch.is_sdxl_like)
        self.edit_mode_switch.setEnabled(self.model.can_toggle_edit)

    def update_context_layers(self):
        current = self.context_combo.currentData()
        with theme.SignalBlocker(self.context_combo):
            while self.context_combo.count() > 3:
                self.context_combo.removeItem(self.context_combo.count() - 1)
            icon = theme.icon("context-layer")
            for layer in self._model.layers.masks:
                self.context_combo.addItem(icon, f"{layer.name}", layer.id)
        current_index = self.context_combo.findData(current)
        if current_index >= 0:
            self.context_combo.setCurrentIndex(current_index)

    def update_context(self):
        if self._model.inpaint.context == InpaintContext.layer_bounds:
            i = self.context_combo.findData(self._model.inpaint.context_layer_id)
            self.context_combo.setCurrentIndex(i)
        else:
            i = self.context_combo.findData(self._model.inpaint.context)
            self.context_combo.setCurrentIndex(i)

    def set_context(self):
        data = self.context_combo.currentData()
        if isinstance(data, QUuid):
            self._model.inpaint.context = InpaintContext.layer_bounds
            self._model.inpaint.context_layer_id = data
        elif isinstance(data, InpaintContext):
            self._model.inpaint.context = data


class ProgressBar(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._model = root.active_model
        self._model_bindings: list[QMetaObject.Connection] = []

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self.setLayout(layout)

        self._bar = QProgressBar(self)
        self._bar.setMinimum(0)
        self._bar.setMaximum(1000)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        self._bar_palette = self._bar.palette()
        layout.addWidget(self._bar)

        self._detail_label = QLabel(self)
        self._detail_label.setStyleSheet(f"color: {theme.grey}; font-size: 11px;")
        self._detail_label.setVisible(False)
        layout.addWidget(self._detail_label)

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        if self._model != model:
            Binding.disconnect_all(self._model_bindings)
            self._model = model
            self._model_bindings = [
                self._model.progress_changed.connect(self._update_progress),
                self._model.progress_kind_changed.connect(self._update_progress_kind),
                self._model.progress_details_changed.connect(self._update_details),
            ]

    def _update_progress_kind(self):
        palette = self._bar_palette
        if self._model.progress_kind is ProgressKind.upload:
            palette = self._bar.palette()
            palette.setColor(QPalette.ColorRole.Highlight, QColor(theme.progress_alt))
        self._bar.setPalette(palette)

    def _update_progress(self):
        if self._model.progress >= 0:
            self._bar.setValue(int(self._model.progress * 1000))
        else:
            if self._bar.value() >= 100:
                self._bar.reset()
            self._bar.setValue(min(99, self._bar.value() + 2))

    def _update_details(self):
        details = self._model.progress_details
        if not details.current_node:
            self._detail_label.setVisible(False)
            return
        text = details.current_node
        if details.sample_step > 0 and details.sample_max > 0:
            text += f"  {details.sample_step}/{details.sample_max}"
        self._detail_label.setText(text)
        self._detail_label.setVisible(True)


class GenerationWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._model: Model = root.active_model
        self._model_bindings: list[QMetaObject.Connection | Binding] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 2, 0)
        self.setLayout(layout)

        self.workspace_select = WorkspaceSelectWidget(self)
        self.style_select = StyleSelectWidget(self)

        style_layout = QHBoxLayout()
        style_layout.addWidget(self.workspace_select)
        style_layout.addWidget(self.style_select)
        layout.addLayout(style_layout)

        self.region_prompt = RegionPromptWidget(self)
        layout.addWidget(self.region_prompt)

        self.strength_slider = StrengthWidget(parent=self, label=_("Denoise"))
        self.strength_slider.setToolTip(
            _("How strongly the current image is changed when refining or editing.")
        )
        self.layer_count_widget = LayerCountWidget(self)
        self.layer_count_widget.setVisible(False)
        self.add_region_button = create_wide_tool_button("region-add", _("Add Region"), self)
        self.add_control_button = create_wide_tool_button(
            "control-add", _("Add Control Layer"), self
        )
        strength_layout = QHBoxLayout()
        strength_layout.addWidget(self.strength_slider)
        strength_layout.addWidget(self.layer_count_widget)
        strength_layout.addWidget(self.add_control_button)
        strength_layout.addWidget(self.add_region_button)
        layout.addLayout(strength_layout)

        self.style_params = StyleParamsWidget(self)
        layout.addWidget(self.style_params)

        self.lora_panel = LoraDockerPanel(self)
        layout.addWidget(self.lora_panel)

        self.custom_inpaint = CustomInpaintWidget(self)
        layout.addWidget(self.custom_inpaint)

        self.quick_style_bar = QuickStyleBar(self)
        layout.addWidget(self.quick_style_bar)

        self.generate_button = GenerateButton(JobKind.diffusion, self)

        self.inpaint_mode_button = QToolButton(self)
        self.inpaint_mode_button.setArrowType(Qt.ArrowType.DownArrow)
        self.inpaint_mode_button.setFixedHeight(self.generate_button.minimumSizeHint().height() - 3)
        self.inpaint_mode_button.clicked.connect(self.show_inpaint_menu)
        self.generate_menu = self._create_generate_menu()
        self.inpaint_menu = self._create_inpaint_menu()
        self.refine_menu = self._create_refine_menu()
        self.refine_selection_menu = self._create_refine_selection_menu()
        self.generate_region_menu = self._create_generate_region_menu()
        self.refine_region_menu = self._create_refine_region_menu()
        self.edit_menu = self._create_edit_menu()

        self.region_mask_button = QToolButton(self)
        self.region_mask_button.setIcon(theme.icon("region-alpha"))
        self.region_mask_button.setCheckable(True)
        self.region_mask_button.setFixedHeight(self.generate_button.height() - 2)
        self.region_mask_button.setToolTip(
            _("Generate the active layer region only (use layer transparency as mask)")
        )

        generate_layout = QHBoxLayout()
        generate_layout.setSpacing(0)
        generate_layout.addWidget(self.generate_button)
        generate_layout.addWidget(self.inpaint_mode_button)
        generate_layout.addWidget(self.region_mask_button)

        self.queue_button = QueueButton(parent=self)
        self.queue_button.setFixedHeight(self.generate_button.height() - 2)

        self.cancel_button = QToolButton(self)
        self.cancel_button.setIcon(theme.icon("cancel"))
        self.cancel_button.setToolTip(_("Cancel all active and queued jobs"))
        self.cancel_button.setFixedHeight(self.generate_button.height() - 2)
        self.cancel_button.clicked.connect(actions.cancel_all)

        actions_layout = QHBoxLayout()
        actions_layout.addLayout(generate_layout)
        actions_layout.addWidget(self.cancel_button)
        actions_layout.addWidget(self.queue_button)
        layout.addLayout(actions_layout)

        self.progress_bar = ProgressBar(self)
        layout.addWidget(self.progress_bar)

        self.vram_widget = VramWidget(self)
        layout.addWidget(self.vram_widget)

        self.error_box = ErrorBox(self)
        layout.addWidget(self.error_box)

        self.history = HistoryWidget(self)
        self.history.item_activated.connect(self.apply_result)
        layout.addWidget(self.history)

        self.update_generate_options()

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
                bind(model, "style", self.style_select, "value"),
                bind(model, "strength", self.strength_slider, "value"),
                bind(model, "layer_count", self.layer_count_widget, "value"),
                bind(model, "error", self.error_box, "error", Bind.one_way),
                bind_toggle(model, "region_only", self.region_mask_button),
                model.inpaint.mode_changed.connect(self.update_generate_options),
                model.strength_changed.connect(self.update_generate_options),
                model.document.selection_bounds_changed.connect(self.update_generate_options),
                model.document.layers.active_changed.connect(self.update_generate_options),
                model.regions.active_changed.connect(self.update_generate_options),
                model.region_only_changed.connect(self.update_generate_options),
                model.style_changed.connect(self.update_generate_options),
                model.edit_mode_changed.connect(self.update_generate_options),
                self.add_control_button.clicked.connect(self.add_control),
                self.add_region_button.clicked.connect(self.add_region),
                self.region_prompt.activated.connect(model.generate),
                self.generate_button.clicked.connect(model.generate),
                self.generate_button.ctrl_clicked.connect(model.generate_replace),
            ]
            self.region_prompt.regions = model.active_regions
            self.custom_inpaint.model = model
            self.style_params.model = model
            self.lora_panel.model = model
            self.quick_style_bar.model = model
            self.generate_button.model = model
            self.queue_button.model = model
            self.progress_bar.model = model
            self.strength_slider.model = model
            self.history.model_ = model
            self.update_generate_options()

    def apply_result(self, item: QListWidgetItem):
        job_id, index = self.history.item_info(item)
        self.model.apply_generated_result(job_id, index)

    _inpaint_text: ClassVar[dict[InpaintMode, str]] = {
        InpaintMode.automatic: _("Default (Auto-detect)"),
        InpaintMode.fill: _("Fill"),
        InpaintMode.expand: _("Expand"),
        InpaintMode.add_object: _("Add Content"),
        InpaintMode.remove_object: _("Remove Content"),
        InpaintMode.replace_background: _("Replace Background"),
        InpaintMode.custom: _("Generate (Custom)"),
    }

    def _mk_action(self, mode: InpaintMode, text: str, icon: str, is_edit: bool | None = False):
        action = QAction(text, self)
        action.setIcon(theme.icon(icon))
        action.setIconVisibleInMenu(True)
        action.triggered.connect(lambda: self.change_inpaint_mode(mode, is_edit))
        return action

    def _create_generate_menu(self):
        menu = QMenu(self)
        menu.addAction(
            self._mk_action(InpaintMode.automatic, _("Generate"), "workspace-generation")
        )
        menu.addAction(self._mk_action(InpaintMode.automatic, _("Edit"), "edit", is_edit=True))
        return menu

    def _create_inpaint_menu(self):
        menu = QMenu(self)

        def add(mode: InpaintMode, text: str, icon: str, is_edit: bool | None = False):
            text = text or self._inpaint_text[mode]
            menu.addAction(self._mk_action(mode, text, icon, is_edit))

        add(InpaintMode.automatic, "", "inpaint-automatic")
        add(InpaintMode.fill, "", "inpaint-fill")
        add(InpaintMode.expand, "", "inpaint-expand")
        add(InpaintMode.add_object, "", "inpaint-add_object")
        add(InpaintMode.remove_object, "", "inpaint-remove_object")
        add(InpaintMode.replace_background, "", "inpaint-replace_background")
        add(InpaintMode.add_object, _("Edit"), "edit", is_edit=True)
        add(InpaintMode.custom, "", "inpaint-custom", is_edit=None)
        return menu

    def _create_generate_region_menu(self):
        menu = QMenu(self)
        menu.addAction(
            self._mk_action(InpaintMode.automatic, _("Generate Region"), "generate-region")
        )
        menu.addAction(
            self._mk_action(
                InpaintMode.custom, _("Generate Region (Custom)"), "inpaint-custom", is_edit=None
            )
        )
        return menu

    def _create_refine_menu(self):
        menu = QMenu(self)
        menu.addAction(self._mk_action(InpaintMode.automatic, _("Refine"), "refine"))
        menu.addAction(self._mk_action(InpaintMode.automatic, _("Edit"), "edit", is_edit=True))
        return menu

    def _create_refine_selection_menu(self):
        menu = QMenu(self)
        menu.addAction(self._mk_action(InpaintMode.automatic, _("Refine"), "refine"))
        menu.addAction(self._mk_action(InpaintMode.automatic, _("Edit"), "edit", is_edit=True))
        menu.addAction(
            self._mk_action(
                InpaintMode.custom, _("Refine (Custom)"), "inpaint-custom", is_edit=None
            )
        )
        return menu

    def _create_refine_region_menu(self):
        menu = QMenu(self)
        menu.addAction(self._mk_action(InpaintMode.automatic, _("Refine Region"), "refine-region"))
        menu.addAction(
            self._mk_action(InpaintMode.custom, _("Refine Region (Custom)"), "inpaint-custom")
        )
        return menu

    def _create_edit_menu(self):
        menu = QMenu(self)
        menu.addAction(self._mk_action(InpaintMode.automatic, _("Edit"), "edit"))
        menu.addAction(self._mk_action(InpaintMode.custom, _("Edit (Custom)"), "inpaint-custom"))
        return menu

    def show_inpaint_menu(self):
        width = self.generate_button.width() + self.inpaint_mode_button.width()
        pos = QPoint(0, self.generate_button.height())
        if not self.model.edit_mode and self.model.arch.is_edit:
            menu = self.edit_menu
        elif self.model.strength == 1.0:
            if self.model.region_only:
                menu = self.generate_region_menu
            elif self.model.document.selection_bounds:
                menu = self.inpaint_menu
                menu.actions()[-2].setEnabled(self.model.can_edit)
            else:
                menu = self.generate_menu
        else:
            if self.model.region_only:
                menu = self.refine_region_menu
            elif self.model.document.selection_bounds:
                menu = self.refine_selection_menu
                menu.actions()[1].setEnabled(self.model.can_edit)
            else:
                menu = self.refine_menu
                menu.actions()[1].setEnabled(self.model.can_edit)

        menu.setFixedWidth(width)
        menu.exec_(self.generate_button.mapToGlobal(pos))

    def change_inpaint_mode(self, mode: InpaintMode, is_edit: bool | None):
        self.model.inpaint.mode = mode
        if is_edit is not None:
            self.model.edit_mode = is_edit

    def toggle_region_only(self, checked: bool):
        self.model.region_only = checked

    def add_region(self):
        self.model.active_regions.create_region_group()

    def add_control(self):
        self.model.active_regions.add_control()

    def update_generate_options(self):
        if not self.model.has_document:
            return

        arch = self.model.arch
        self.strength_slider.setVisible(arch is not Arch.qwen_l)
        self.layer_count_widget.setVisible(arch is Arch.qwen_l)

        regions = self.model.active_regions
        self.region_prompt.regions = regions

        has_regions = len(regions) > 0
        has_active_region = regions.is_linked(self.model.layers.active)
        is_region_only = has_regions and has_active_region and self.model.region_only
        is_edit = self.model.is_editing
        self.region_mask_button.setVisible(has_regions)
        self.region_mask_button.setEnabled(has_active_region)
        self.region_mask_button.setIcon(_region_mask_button_icons[is_region_only])

        if self.model.document.selection_bounds is None and not is_region_only:
            self.inpaint_mode_button.setVisible(self.model.can_toggle_edit)
            self.custom_inpaint.setVisible(False)
            if is_edit:
                icon = "edit"
                text = _("Edit")
            elif self.model.strength == 1.0:
                icon = "workspace-generation"
                text = _("Generate")
            else:
                icon = "refine"
                text = _("Refine")
        else:
            self.inpaint_mode_button.setVisible(True)
            self.custom_inpaint.setVisible(self.model.inpaint.mode is InpaintMode.custom)
            mode = self.model.resolve_inpaint_mode()
            text = _("Generate")
            if is_edit:
                text = _("Edit")
            elif self.model.strength < 1:
                text = _("Refine")
            if is_region_only:
                text += " " + _("Region")
            if mode is InpaintMode.custom:
                text += " " + _("(Custom)")
            if self.model.strength == 1.0 and not is_edit:
                if mode is InpaintMode.custom:
                    icon = "inpaint-custom"
                elif is_region_only:
                    icon = "generate-region"
                else:
                    icon = f"inpaint-{mode.name}"
                    text = self._inpaint_text[mode]
            elif not is_edit:
                if mode is InpaintMode.custom:
                    icon = "inpaint-custom"
                elif is_region_only:
                    icon = "refine-region"
                else:
                    icon = "refine"
            else:
                if mode is InpaintMode.custom:
                    icon = "inpaint-custom"
                else:
                    icon = "edit"

        self.generate_button.operation = text
        self.generate_button.setIcon(theme.icon(icon))


_region_mask_button_icons = {
    True: theme.icon("region-alpha-active"),
    False: theme.icon("region-alpha"),
}
