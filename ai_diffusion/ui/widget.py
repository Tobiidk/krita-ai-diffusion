from __future__ import annotations

from collections.abc import Callable
from itertools import chain
from textwrap import fill
from typing import Any, ClassVar, cast

from krita import Krita
from PyQt5.QtCore import QEvent, QMetaObject, QPoint, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import (
    QCloseEvent,
    QColor,
    QDesktopServices,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPaintDevice,
    QPainter,
    QPaintEvent,
    QPalette,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PyQt5.QtWidgets import (
    QAction,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QScrollBar,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QStyleOption,
    QToolButton,
    QWidget,
    QWidgetAction,
)

from ..client import filter_supported_styles, resolve_arch
from ..connection import ConnectionState
from ..jobs import JobKind, JobState
from ..localization import translate as _
from ..model import (
    Error,
    ErrorKind,
    Model,
    ProgressKind,
    QueueMode,
    SamplingQuality,
    Workspace,
    no_error,
)
from ..properties import Bind, Binding, bind, bind_combo
from ..root import root
from ..server import ServerState
from ..settings import ServerMode, Settings, settings
from ..style import SamplerPresets, Style, Styles, sort_recent_styles
from ..text import (
    char16_index_to_str_index,
    char16_len,
    edit_attention,
    pattern_comment,
    pattern_layer,
    pattern_lora,
    pattern_weight_expr,
    pattern_wildcard,
    select_on_cursor_pos,
    str_index_to_char16_index,
)
from ..util import ensure
from ..workflow import apply_denoise_strength, snap_to_percent
from .. import eventloop
from . import actions, theme
from .autocomplete import PromptAutoComplete
from .settings_widgets import NoWheelComboBox
from .switch import SwitchWidget
from .theme import SignalBlocker


class QueuePopup(QMenu):
    _model: Model
    _connections: list[QMetaObject.Connection]

    def __init__(self, supports_batch=True, parent: QWidget | None = None):
        super().__init__(parent)
        self._connections = []

        palette = self.palette()
        self.setObjectName("QueuePopup")
        self.setStyleSheet(
            f"""
            QWidget#QueuePopup {{
                background-color: {palette.window().color().name()}; 
                border: 1px solid {palette.dark().color().name()};
            }}"""
        )

        self._layout = QGridLayout()
        self.setLayout(self._layout)

        counts_label = QLabel(_("Jobs"), self)
        counts_layout = QHBoxLayout()
        counts_layout.setContentsMargins(0, 0, 0, 0)
        counts_layout.addWidget(QLabel(_("Document:"), self))
        self._counts_document = QLabel("0", self)
        self._counts_document.setStyleSheet(f"color: {theme.highlight}; font-weight: bold;")
        counts_layout.addWidget(self._counts_document)
        counts_layout.addWidget(QLabel(_("Total:"), self))
        counts_layout.addSpacing(4)
        self._counts_total = QLabel("0", self)
        self._counts_total.setStyleSheet(f"color: {theme.highlight}; font-weight: bold;")
        counts_layout.addWidget(self._counts_total)
        counts_layout.addStretch()
        self._layout.addWidget(counts_label, 0, 0)
        self._layout.addLayout(counts_layout, 0, 1)

        batch_label = QLabel(_("Batches"), self)
        batch_label.setVisible(supports_batch)
        self._layout.addWidget(batch_label, 1, 0)
        batch_layout = QHBoxLayout()
        self._batch_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._batch_slider.setMinimum(1)
        self._batch_slider.setMaximum(10)
        self._batch_slider.setSingleStep(1)
        self._batch_slider.setPageStep(1)
        self._batch_slider.setVisible(supports_batch)
        self._batch_slider.setToolTip(_("Number of jobs to enqueue at once"))
        self._batch_label = QLabel("1", self)
        self._batch_label.setVisible(supports_batch)
        batch_layout.addWidget(self._batch_slider)
        batch_layout.addWidget(self._batch_label)
        self._layout.addLayout(batch_layout, 1, 1)

        self._seed_label = QLabel(_("Seed"), self)
        self._layout.addWidget(self._seed_label, 2, 0)
        self._seed_input = QDoubleSpinBox(self)
        self._seed_check = QCheckBox(self)
        self._seed_check.setText(_("Fixed"))
        self._seed_input.setMinimum(0)
        self._seed_input.setMaximum(2**32 - 1)
        self._seed_input.setDecimals(0)
        self._seed_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._seed_input.setToolTip(
            _(
                "The seed controls the random part of the output. A fixed seed value will always produce the same result for the same inputs."
            )
        )
        self._randomize_seed = QToolButton(self)
        self._randomize_seed.setIcon(theme.icon("random"))
        seed_layout = QHBoxLayout()
        seed_layout.addWidget(self._seed_check)
        seed_layout.addWidget(self._seed_input)
        seed_layout.addWidget(self._randomize_seed)
        self._layout.addLayout(seed_layout, 2, 1)

        resolution_multiplier_label = QLabel(_("Resolution"), self)
        self._resolution_multiplier_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._resolution_multiplier_slider.setRange(3, 15)
        self._resolution_multiplier_slider.setValue(10)
        self._resolution_multiplier_slider.setSingleStep(1)
        self._resolution_multiplier_slider.setPageStep(1)
        self._resolution_multiplier_slider.setToolTip(Settings._resolution_multiplier.desc)
        self._resolution_multiplier_slider.valueChanged.connect(self._set_resolution_multiplier)
        self._resolution_multiplier_display = QLabel("1.0 x", self)
        self._resolution_multiplier_display.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._resolution_multiplier_display.setMinimumWidth(20)
        resolution_multiplier_layout = QHBoxLayout()
        resolution_multiplier_layout.addWidget(self._resolution_multiplier_slider)
        resolution_multiplier_layout.addWidget(self._resolution_multiplier_display)
        self._layout.addWidget(resolution_multiplier_label, 3, 0)
        self._layout.addLayout(resolution_multiplier_layout, 3, 1)

        enqueue_label = QLabel(_("Enqueue"), self)
        self._queue_mode_combo = QComboBox(self)
        self._queue_mode_combo.addItem(_("at the Back"), QueueMode.back)
        self._queue_mode_combo.addItem(_("in Front (new jobs first)"), QueueMode.front)
        self._queue_mode_combo.addItem(_("Replace Queue"), QueueMode.replace)
        self._layout.addWidget(enqueue_label, 4, 0)
        self._layout.addWidget(self._queue_mode_combo, 4, 1)

        cancel_label = QLabel(_("Cancel"), self)
        self._layout.addWidget(cancel_label, 5, 0)
        self._cancel_active = self._create_cancel_button(_("Active"), actions.cancel_active)
        self._cancel_queued = self._create_cancel_button(_("Queued"), actions.cancel_queued)
        self._cancel_all = self._create_cancel_button(_("All"), actions.cancel_all)
        cancel_layout = QHBoxLayout()
        cancel_layout.addWidget(self._cancel_active)
        cancel_layout.addWidget(self._cancel_queued)
        cancel_layout.addWidget(self._cancel_all)
        self._layout.addLayout(cancel_layout, 5, 1)

        self._model = root.active_model

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        Binding.disconnect_all(self._connections)
        self._model = model
        self._randomize_seed.setEnabled(model.fixed_seed)
        self._seed_input.setValue(model.seed)
        self._seed_input.setEnabled(model.fixed_seed)
        self._batch_label.setText(str(model.batch_count))
        self._connections = [
            bind(model, "batch_count", self._batch_slider, "value"),
            model.batch_count_changed.connect(lambda v: self._batch_label.setText(str(v))),
            model.seed_changed.connect(lambda: self._seed_input.setValue(self._model.seed)),
            self._seed_input.valueChanged.connect(lambda v: setattr(self._model, "seed", int(v))),
            bind(model, "fixed_seed", self._seed_check, "checked", Bind.one_way),
            self._seed_check.toggled.connect(lambda v: setattr(self._model, "fixed_seed", v)),
            model.fixed_seed_changed.connect(self._seed_input.setEnabled),
            model.fixed_seed_changed.connect(self._randomize_seed.setEnabled),
            self._randomize_seed.clicked.connect(model.generate_seed),
            model.resolution_multiplier_changed.connect(self._update_resolution_multiplier),
            bind_combo(model, "queue_mode", self._queue_mode_combo),
            model.jobs.count_changed.connect(self._update_job_count),
        ]
        self._update_job_count()

    def _create_cancel_button(self, name: str, action: Callable[[], None]):
        button = QToolButton(self)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setText(name)
        button.setIcon(theme.icon("cancel"))
        button.setEnabled(False)
        button.clicked.connect(action)
        return button

    def _update_job_count(self):
        has_active = self._model.jobs.any_executing()
        n_queued = self._model.jobs.count(JobState.queued)
        n_total = sum(m.jobs.count(JobState.queued) for m in root.models)
        self._counts_document.setText(str(n_queued + (1 if has_active else 0)))
        self._counts_total.setText(str(n_total + (1 if has_active else 0)))
        self._cancel_active.setEnabled(has_active)
        self._cancel_queued.setEnabled(n_queued > 0)
        self._cancel_all.setEnabled(has_active or n_queued > 0)

    def _update_resolution_multiplier(self):
        slider_value = round(self.model.resolution_multiplier * 10)
        if self._resolution_multiplier_slider.value() != slider_value:
            self._resolution_multiplier_slider.setValue(slider_value)

    def _set_resolution_multiplier(self, value: int):
        self.model.resolution_multiplier = value / 10
        self._resolution_multiplier_display.setText(f"{(value / 10):.1f} x")

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        if parent := cast(QWidget, self.parent()):
            parent.close()
        return super().closeEvent(a0)


class QueueButton(QToolButton):
    def __init__(self, supports_batch=True, parent: QWidget | None = None):
        super().__init__(parent)
        self._model = root.active_model

        self._popup = QueuePopup(supports_batch)
        popup_action = QWidgetAction(self)
        popup_action.setDefaultWidget(self._popup)
        self.addAction(popup_action)

        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._connect_model()

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        if self._model != model:
            Binding.disconnect_all(self._connections)
            self._model = model
            self._popup.model = model
            self._connect_model()

    def _connect_model(self):
        self._connections = [
            self._model.jobs.count_changed.connect(self._update),
            self._model.progress_kind_changed.connect(self._update),
        ]
        self._update()

    def _update(self):
        count = self._model.jobs.count(JobState.queued)
        queued_msg = _("{count} jobs queued.", count=count)
        cancel_msg = _("Click to cancel.")

        if self._model.progress_kind is ProgressKind.upload:
            self.setIcon(theme.icon("queue-upload"))
            self.setToolTip(_("Uploading models.") + f" {queued_msg} {cancel_msg}")
            count += 1
        elif self._model.jobs.any_executing():
            self.setIcon(theme.icon("queue-active"))
            if count > 0:
                self.setToolTip(_("Generating image.") + f" {queued_msg} {cancel_msg}")
            else:
                self.setToolTip(_("Generating image.") + f" {cancel_msg}")
            count += 1
        elif count > 0:
            self.setIcon(theme.icon("queue-waiting"))
            self.setToolTip(f"{queued_msg} {cancel_msg}")
        else:
            self.setIcon(theme.icon("queue-inactive"))
            self.setToolTip(_("Idle."))

        self.setText(f"{count} ")

    def sizeHint(self) -> QSize:
        original = super().sizeHint()
        width = original.height() * 0.75 + self.fontMetrics().width(" 99 ") + 20
        return QSize(int(width), original.height())

    def paintEvent(self, a0):
        _paint_tool_drop_down(self, self.text())


class StyleSelectWidget(QWidget):
    value_changed = pyqtSignal(Style)
    quality_changed = pyqtSignal(SamplingQuality)

    def __init__(self, parent: QWidget | None, show_quality=False):
        super().__init__(parent)
        self._styles: list[Style] = []
        self._value = Styles.list().default

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self._combo = QComboBox(self)
        self.update_styles()
        self._combo.currentIndexChanged.connect(self.change_style)
        layout.addWidget(self._combo, 3)

        if show_quality:
            self._quality_combo = QComboBox(self)
            self._quality_combo.addItem(_("Fast"), SamplingQuality.fast.value)
            self._quality_combo.addItem(_("Quality"), SamplingQuality.quality.value)
            self._quality_combo.currentIndexChanged.connect(self.change_quality)
            layout.addWidget(self._quality_combo, 1)

        settings_btn = QToolButton(self)
        settings_btn.setIcon(theme.icon("settings"))
        settings_btn.setAutoRaise(True)
        settings_btn.clicked.connect(self.show_settings)
        layout.addWidget(settings_btn)

        Styles.list().changed.connect(self.update_styles)
        Styles.list().name_changed.connect(self.update_styles)
        root.connection.state_changed.connect(self.update_styles)
        settings.changed.connect(self._on_settings_changed)

    def update_styles(self):
        if root.connection.state is not ConnectionState.connected:
            return
        client = root.connection.client_if_connected
        filtered = filter_supported_styles(Styles.list().filtered(), client)
        recent, remaining = sort_recent_styles(
            filtered, settings.recent_styles, settings.recent_styles_count
        )
        if self._value not in chain(recent, remaining):
            recent.insert(0, self._value)
        self._styles = recent + remaining
        with SignalBlocker(self._combo):
            self._combo.clear()
            for style in recent:
                icon = theme.checkpoint_icon(resolve_arch(style, client))
                self._combo.addItem(icon, f"{style.name} ★", style.filename)
            if recent and remaining:
                self._combo.insertSeparator(len(recent))
            for style in remaining:
                icon = theme.checkpoint_icon(resolve_arch(style, client))
                self._combo.addItem(icon, style.name, style.filename)
            self._combo.setCurrentText(self._value.name)

    def change_style(self):
        filename = self._combo.currentData()
        if filename is None:
            return  # separator item selected
        style = next((s for s in self._styles if s.filename == filename), None)
        if style is None or style == self._value:
            return
        self._value = style
        self.value_changed.emit(style)

    def change_quality(self):
        quality = SamplingQuality(self._quality_combo.currentData())
        self.quality_changed.emit(quality)

    def _on_settings_changed(self, name: str, value: object):
        if "recent_styles" in name:
            self.update_styles()

    def show_settings(self):
        from .settings import SettingsDialog

        SettingsDialog.instance().show(self._value)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, style: Style):
        if style != self._value:
            self._value = style
            if style not in self._styles:
                self.update_styles()
            else:
                idx = self._combo.findData(style.filename)
                self._combo.setCurrentIndex(idx)


class PromptHighlighter(QSyntaxHighlighter):
    def __init__(self, parent):
        super().__init__(parent)

        self._comment_fmt = QTextCharFormat()
        self._comment_fmt.setForeground(QColor(theme.grey))
        self._comment_fmt.setFontItalic(True)

        self._weight_fmt = QTextCharFormat()
        self._weight_fmt.setForeground(QColor(theme.red))

        self._keyword_fmt = QTextCharFormat()
        self._keyword_fmt.setForeground(QColor(theme.strong_highlight))

        self._grey_fmt = QTextCharFormat()
        self._grey_fmt.setForeground(QColor(theme.grey))

    def highlightBlock(self, text: str | None):
        if text is None:
            return
        comment_start = len(text)
        m = pattern_comment.search(text)
        if m:
            comment_start = m.start()
            self.setFormat(m.start(), m.end() - m.start(), self._comment_fmt)

        for m in pattern_weight_expr.finditer(text):
            if m.start(1) < comment_start:
                self.setFormat(m.start(), 1, self._grey_fmt)
                self.setFormat(m.start(1), m.end(1) - m.start(1), self._weight_fmt)
                self.setFormat(m.end(1), 1, self._grey_fmt)

        for m in pattern_lora.finditer(text):
            if m.start() < comment_start:
                # keyword "lora" starts at m.start()+1 (after '<'), length 4
                self.setFormat(m.start(), 1, self._grey_fmt)
                self.setFormat(m.start() + 1, 4, self._keyword_fmt)
                self.setFormat(m.end(1), 1, self._grey_fmt)
                if m.group(2):
                    self.setFormat(m.start(2), m.end(2) - m.start(2), self._weight_fmt)
                    self.setFormat(m.end(2), 1, self._grey_fmt)

        for m in pattern_layer.finditer(text):
            if m.start() < comment_start:
                # keyword "layer" starts at m.start()+1 (after '<'), length 5
                self.setFormat(m.start(), 1, self._grey_fmt)
                self.setFormat(m.start() + 1, 5, self._keyword_fmt)
                self.setFormat(m.end(1), 1, self._grey_fmt)

        for m in pattern_wildcard.finditer(text):
            if m.start() < comment_start:
                wildcard_text = m.group(0)
                wildcard_start = m.start()
                self.setFormat(wildcard_start, 1, self._keyword_fmt)
                for i, char in enumerate(wildcard_text):
                    if char == "|":
                        self.setFormat(wildcard_start + i, 1, self._keyword_fmt)
                self.setFormat(wildcard_start + len(wildcard_text) - 1, 1, self._keyword_fmt)


class ResizeHandle(QWidget):
    """A small resize handle that appears at the bottom of the prompt widget."""

    handle_dragged = pyqtSignal(int)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setFixedSize(22, 8)
        self._dragging = False

    def mousePressEvent(self, a0: QMouseEvent | None) -> None:
        if ensure(a0).button() == Qt.MouseButton.LeftButton:
            self._dragging = True

    def mouseReleaseEvent(self, a0: QMouseEvent | None) -> None:
        self._dragging = False

    def mouseMoveEvent(self, a0: QMouseEvent | None) -> None:
        if not self._dragging:
            return
        y_pos = self.mapToParent(ensure(a0).pos()).y()
        self.handle_dragged.emit(y_pos)

    def paintEvent(self, a0: QPaintEvent | None) -> None:
        if not self.isVisible():
            return
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText).lighter(100))
        painter.setBrush(painter.pen().color())
        w, h = self.width(), self.height()
        for i, x in enumerate(range(2, w - 1, 3)):
            y = 2 * h // 3 if i % 2 == 0 else h // 3
            painter.drawEllipse(x - 1, y - 1, 2, 2)


class TextPromptWidget(QPlainTextEdit):
    activated = pyqtSignal()
    text_changed = pyqtSignal(str)
    handle_dragged = pyqtSignal(int)

    def __init__(self, line_count=2, is_negative=False, parent=None):
        super().__init__(parent)
        self._line_count = line_count
        self._is_negative = is_negative

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.setFrameStyle(QFrame.Shape.NoFrame)

        self._completer = PromptAutoComplete(self)
        self.textChanged.connect(self.notify_text_changed)

        self._highlighter = PromptHighlighter(self.document())
        self._resize_handle: ResizeHandle | None = None

        palette: QPalette = self.palette()
        self._base_color = palette.color(QPalette.ColorRole.Base)
        self.is_negative = is_negative
        self.line_count = line_count

    def event(self, e: QEvent | None):
        assert e is not None
        # Ctrl+Backspace should be handled by QPlainTextEdit, not Krita.
        if e.type() == QEvent.Type.ShortcutOverride:
            assert isinstance(e, QKeyEvent)
            if e.matches(QKeySequence.StandardKey.DeleteStartOfWord):
                e.accept()
        return super().event(e)

    def keyPressEvent(self, e: QKeyEvent | None):
        assert e is not None
        if self._completer.is_active and e.key() in PromptAutoComplete.action_keys:
            e.ignore()
            return

        self.handle_weight_adjustment(e)

        if e.key() == Qt.Key.Key_Return and e.modifiers() == Qt.KeyboardModifier.ShiftModifier:
            self.activated.emit()
        else:
            super().keyPressEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._resize_handle:
            self._place_resize_handle()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        if scroll := self.verticalScrollBar():
            scroll.triggerAction(QScrollBar.SliderAction.SliderToMinimum)

    def focusNextPrevChild(self, next):
        if self._completer.is_active:
            return False
        return super().focusNextPrevChild(next)

    def notify_text_changed(self):
        self._completer.check_completion()
        self.text_changed.emit(self.text)

    @property
    def text(self):
        return self.toPlainText()

    @text.setter
    def text(self, value: str):
        if value == self.text:
            return
        with SignalBlocker(self):  # avoid auto-completion on non-user input
            self.setPlainText(value)

    @property
    def is_resizable(self):
        return self._resize_handle is not None

    @is_resizable.setter
    def is_resizable(self, value: bool):
        if value and self._resize_handle is None:
            self._resize_handle = ResizeHandle(self)
            self._resize_handle.handle_dragged.connect(self.handle_dragged)
            self._place_resize_handle()
            self._resize_handle.show()
        if not value and self._resize_handle is not None:
            self._resize_handle.handle_dragged.disconnect(self.handle_dragged)
            self._resize_handle.deleteLater()
            self._resize_handle = None

    def _place_resize_handle(self):
        if self._resize_handle:
            rect = self.geometry()
            self._resize_handle.move(
                (rect.width() - self._resize_handle.width()) // 2,
                rect.height() - self._resize_handle.height(),
            )

    @property
    def line_count(self):
        return self._line_count

    @line_count.setter
    def line_count(self, value: int):
        self._line_count = value
        fm = QFontMetrics(ensure(self.document()).defaultFont())
        self.setFixedHeight(fm.lineSpacing() * value + 10)

    @property
    def is_negative(self):
        return self._is_negative

    @is_negative.setter
    def is_negative(self, value: bool):
        self._is_negative = value
        if not value:
            self.setPlaceholderText(_("Describe the content you want to see, or leave empty."))
        else:
            self.setPlaceholderText(_("Describe content you want to avoid."))

        if value:
            self.setContentsMargins(0, 2, 0, 2)
            self.setFrameStyle(QFrame.Shape.StyledPanel)
            self.setStyleSheet("QFrame { background: rgba(255, 0, 0, 15); }")
        else:
            self.setFrameStyle(QFrame.Shape.NoFrame)

    @property
    def has_focus(self):
        return self.hasFocus()

    @has_focus.setter
    def has_focus(self, value: bool):
        if value:
            self.setFocus()

    def move_cursor_to_end(self):
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

    def handle_weight_adjustment(self, event: QKeyEvent):
        """Handles Ctrl + (arrow key up / arrow key down) attention weight adjustment."""
        if event.key() in [Qt.Key.Key_Up, Qt.Key.Key_Down] and (
            event.modifiers() & Qt.Modifier.CTRL
        ):
            cursor = self.textCursor()
            text = self.toPlainText()

            if cursor.hasSelection():
                start = char16_index_to_str_index(text, cursor.selectionStart())
                end = char16_index_to_str_index(text, cursor.selectionEnd())
            else:
                pos = char16_index_to_str_index(text, cursor.position())
                start, end = select_on_cursor_pos(text, pos)

            target_text = text[start:end]
            text_after_edit = edit_attention(target_text, event.key() == Qt.Key.Key_Up)
            text = text[:start] + text_after_edit + text[end:]
            self.setPlainText(text)
            start_c16 = str_index_to_char16_index(text, start)
            cursor = self.textCursor()
            cursor.setPosition(min(start_c16 + char16_len(text_after_edit), char16_len(text)))
            cursor.setPosition(min(start_c16, char16_len(text)), QTextCursor.KeepAnchor)
            self.setTextCursor(cursor)


class StrengthSnapping:
    model: Model

    def __init__(self, model: Model):
        self.model = model

    def get_steps(self) -> tuple[int, int]:
        is_live = self.model.workspace is Workspace.live
        if self.model.workspace is Workspace.animation:
            is_live = self.model.animation.sampling_quality is SamplingQuality.fast
        return self.model.active_style.get_steps(is_live=is_live)

    def nearest_percent(self, value: int) -> int | None:
        _, max_steps = self.get_steps()
        steps, start_at_step = self.apply_strength(value)
        return snap_to_percent(steps, start_at_step, max_steps=max_steps)

    def apply_strength(self, value: int) -> tuple[int, int]:
        strength = value / 100
        _, max_steps = self.get_steps()
        return apply_denoise_strength(strength, steps=max_steps)


# SpinBox variant that allows manually entering strength values,
# but snaps to model_steps on step actions (scrolling, arrows, arrow keys).
class StrengthSpinBox(QSpinBox):
    snapping: StrengthSnapping | None

    def __init__(self, parent=None, minimum=1, maximum=100):
        super().__init__(parent)
        self.snapping = None
        # for manual input
        self.setMinimum(minimum)
        self.setMaximum(maximum)

    def stepBy(self, steps):
        value = max(self.minimum(), min(self.maximum(), self.value() + steps))
        if self.snapping is not None:
            # keep going until we hit a new snap point
            current_point = self.nearest_snap_point(self.value())
            while self.nearest_snap_point(value) == current_point and value > self.minimum():
                value += 1 if steps > 0 else -1
            value = self.nearest_snap_point(value)
        self.setValue(value)

    def nearest_snap_point(self, value: int) -> int:
        assert self.snapping
        return self.snapping.nearest_percent(value) or (int(value / 5) * 5)


class StrengthWidget(QWidget):
    _model: Model | None = None
    _value: int = 100

    value_changed = pyqtSignal(float)

    def __init__(
        self,
        slider_range: tuple[int, int] = (1, 100),
        prefix=True,
        parent=None,
        label: str | None = None,
    ):
        super().__init__(parent)
        self._layout = QHBoxLayout()
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self._layout)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setMinimum(slider_range[0])
        self._slider.setMaximum(slider_range[1])
        self._slider.setValue(self._value)
        self._slider.setSingleStep(5)
        self._slider.valueChanged.connect(self.slider_changed)

        self._input = StrengthSpinBox(
            self, minimum=slider_range[0], maximum=slider_range[1]
        )
        self._input.setValue(self._value)
        if prefix:
            self._input.setPrefix((label or _("Strength")) + ": ")
        self._input.setSuffix("%")
        self._input.setSpecialValueText(_("Off"))
        self._input.valueChanged.connect(self.notify_changed)

        settings.changed.connect(self.update_suffix)

        self._layout.addWidget(self._slider)
        self._layout.addWidget(self._input)

    def slider_changed(self, value: int):
        if self._input.snapping is not None:
            value = self._input.snapping.nearest_percent(value) or value
        self.notify_changed(value)

    def notify_changed(self, value: int):
        if self._update_value(value):
            self.value_changed.emit(self.value)

    def _update_value(self, value: int):
        with SignalBlocker(self._slider), SignalBlocker(self._input):
            self._slider.setValue(value)
            self._input.setValue(value)
        if value != self._value:
            self._value = value
            self.update_suffix()
            return True
        return False

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        if self._model:
            self._model.style_changed.disconnect(self.update_suffix)
            self._model.edit_mode_changed.disconnect(self.update_suffix)
            self._model.animation.sampling_quality_changed.disconnect(self.update_suffix)
        self._model = model
        self._model.style_changed.connect(self.update_suffix)
        self._model.edit_mode_changed.connect(self.update_suffix)
        self._model.animation.sampling_quality_changed.connect(self.update_suffix)
        self._input.snapping = StrengthSnapping(self._model)
        self.update_suffix()

    @property
    def value(self):
        return self._value / 100

    @value.setter
    def value(self, value: float):
        if value == self.value:
            return
        self._update_value(round(value * 100))

    def update_suffix(self):
        if not self._input.snapping or not settings.show_steps:
            self._input.setSuffix("%")
            return
        if self._value <= 0:
            self._input.setSuffix("%")
            return

        steps, start_at_step = self._input.snapping.apply_strength(self._value)
        self._input.setSuffix(f"% ({steps - start_at_step}/{steps})")


class StyleParamsWidget(QWidget):
    """Compact steps/guidance sliders for the main docker, bound to the active style."""

    _model: Model | None = None

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.setLayout(layout)

        # Sampler preset selector for the current quality/live context.
        preset_row = QHBoxLayout()
        preset_row.setContentsMargins(0, 0, 0, 0)
        self._preset_label = QLabel(_("Preset") + ":", self)
        self._preset_label.setFixedWidth(64)
        self._preset_combo = NoWheelComboBox(self)
        self._preset_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLength
        )
        self._preset_combo.activated.connect(self._preset_activated)
        preset_row.addWidget(self._preset_label)
        preset_row.addWidget(self._preset_combo, 1)
        layout.addLayout(preset_row)

        # Steps slider
        steps_row = QHBoxLayout()
        steps_row.setContentsMargins(0, 0, 0, 0)
        self._steps_label = QLabel(_("Steps") + ":", self)
        self._steps_label.setFixedWidth(64)
        self._steps_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._steps_slider.setMinimum(1)
        self._steps_slider.setMaximum(100)
        self._steps_slider.setSingleStep(1)
        self._steps_spin = QSpinBox(self)
        self._steps_spin.setMinimum(1)
        self._steps_spin.setMaximum(100)
        self._steps_spin.setFixedWidth(50)
        self._steps_slider.valueChanged.connect(self._steps_slider_moved)
        self._steps_spin.editingFinished.connect(self._steps_committed)
        steps_row.addWidget(self._steps_label)
        steps_row.addWidget(self._steps_slider)
        steps_row.addWidget(self._steps_spin)
        layout.addLayout(steps_row)

        # Guidance slider
        cfg_row = QHBoxLayout()
        cfg_row.setContentsMargins(0, 0, 0, 0)
        self._cfg_label = QLabel(_("Guidance") + ":", self)
        self._cfg_label.setFixedWidth(64)
        self._cfg_label.setToolTip(_("Guidance Strength (CFG Scale)"))
        self._cfg_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._cfg_slider.setMinimum(10)  # 1.0 * 10
        self._cfg_slider.setMaximum(200)  # 20.0 * 10
        self._cfg_slider.setSingleStep(1)
        self._cfg_spin = QDoubleSpinBox(self)
        self._cfg_spin.setMinimum(1.0)
        self._cfg_spin.setMaximum(20.0)
        self._cfg_spin.setSingleStep(0.1)
        self._cfg_spin.setDecimals(1)
        self._cfg_spin.setFixedWidth(58)
        self._cfg_slider.valueChanged.connect(self._cfg_slider_moved)
        self._cfg_spin.editingFinished.connect(self._cfg_committed)
        cfg_row.addWidget(self._cfg_label)
        cfg_row.addWidget(self._cfg_slider)
        cfg_row.addWidget(self._cfg_spin)
        layout.addLayout(cfg_row)
        self._cfg_widgets = (self._cfg_label, self._cfg_slider, self._cfg_spin)

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        if self._model is not None:
            self._model.effective_style_changed.disconnect(self._read_from_style)
        self._model = model
        model.effective_style_changed.connect(self._read_from_style)
        self._read_from_style()

    def _read_from_style(self):
        """Refresh slider values from the current effective style and workspace."""
        if self._model is None:
            return
        self._sync_preset_combo()
        sampler_name = self._active_sampler_name()
        steps, cfg = self._model.effective_sampler_values()
        supports_guidance = self._supports_guidance()
        with SignalBlocker(self._preset_combo):
            self._preset_combo.setCurrentText(sampler_name)
        for widget in self._cfg_widgets:
            widget.setVisible(supports_guidance)
        with SignalBlocker(self._steps_slider), SignalBlocker(self._steps_spin):
            self._steps_slider.setValue(steps)
            self._steps_spin.setValue(steps)
        if not supports_guidance:
            return
        with SignalBlocker(self._cfg_slider), SignalBlocker(self._cfg_spin):
            self._cfg_slider.setValue(round(cfg * 10))
            self._cfg_spin.setValue(cfg)

    def _sync_preset_combo(self):
        names = SamplerPresets.instance().names()
        current = [self._preset_combo.itemText(i) for i in range(self._preset_combo.count())]
        if current == names:
            return
        with SignalBlocker(self._preset_combo):
            self._preset_combo.clear()
            self._preset_combo.addItems(names)

    def _active_sampler_name(self):
        assert self._model is not None
        style = self._model.active_style
        return style.live_sampler if self._model.is_live_mode else style.sampler

    def _active_preset(self):
        return SamplerPresets.instance()[self._active_sampler_name()]

    def _supports_guidance(self):
        if self._model is None:
            return False
        return self._model.arch.supports_guidance_scale or bool(
            self._active_preset().cfg_schedule
        )

    def _preset_activated(self, index: int = -1):
        if self._model is None:
            return
        name = self._preset_combo.currentText()
        if not name:
            return

        style = self._model.active_style
        is_live = self._model.is_live_mode
        old_name = self._active_sampler_name()
        if old_name == name:
            return

        old_preset = SamplerPresets.instance()[old_name]
        new_preset = SamplerPresets.instance()[name]
        _, current_cfg = self._model.effective_sampler_values()

        if is_live:
            style.live_sampler = name
            if new_preset.cfg_schedule and (
                not old_preset.cfg_schedule or current_cfg <= new_preset.cfg_start
            ):
                style.live_cfg_scale = new_preset.default_guidance
        else:
            style.sampler = name
            if new_preset.cfg_schedule and (
                not old_preset.cfg_schedule or current_cfg <= new_preset.cfg_start
            ):
                style.cfg_scale = new_preset.default_guidance

        style.save()
        self._read_from_style()

    def _steps_slider_moved(self, value: int):
        with SignalBlocker(self._steps_spin):
            self._steps_spin.setValue(value)
        self._write_steps(value)

    def _cfg_slider_moved(self, value: int):
        cfg = value / 10.0
        with SignalBlocker(self._cfg_spin):
            self._cfg_spin.setValue(cfg)
        self._write_cfg(cfg)

    def _steps_committed(self):
        value = self._steps_spin.value()
        with SignalBlocker(self._steps_slider):
            self._steps_slider.setValue(value)
        self._write_steps(value)

    def _cfg_committed(self):
        cfg = self._cfg_spin.value()
        with SignalBlocker(self._cfg_slider):
            self._cfg_slider.setValue(round(cfg * 10))
        self._write_cfg(cfg)

    def _write_steps(self, value: int):
        if self._model is None:
            return
        style = self._model.active_style
        if self._model.is_live_mode:
            style.live_sampler_steps = value
        else:
            style.sampler_steps = value
        style.save()

    def _write_cfg(self, value: float):
        if self._model is None:
            return
        if not self._supports_guidance():
            return
        style = self._model.active_style
        if self._model.is_live_mode:
            style.live_cfg_scale = value
        else:
            style.cfg_scale = value
        style.save()


class LoraDockerItem(QWidget):
    """Single compact LoRA row: name label, strength spinbox, on/off toggle."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.setLayout(layout)

        self._enabled = SwitchWidget(self)
        self._enabled.setChecked(True)
        self._enabled.setToolTip(_("Enable/disable this LoRA"))
        self._enabled.toggled.connect(lambda _: self.changed.emit())

        self._name_label = QLabel(self)
        self._name_label.setTextFormat(Qt.TextFormat.PlainText)
        self._name_label.setMinimumWidth(40)
        self._name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._name_label.setToolTip("")

        self._strength = QSpinBox(self)
        self._strength.setMinimum(-400)
        self._strength.setMaximum(400)
        self._strength.setSingleStep(5)
        self._strength.setValue(100)
        self._strength.setSuffix("%")
        self._strength.setFixedWidth(70)
        self._strength.valueChanged.connect(lambda _: self.changed.emit())

        layout.addWidget(self._enabled)
        layout.addWidget(self._name_label, 1)
        layout.addWidget(self._strength)

    def set_lora(self, lora_dict: dict):
        """Populate from a LoRA dict: {name, strength, enabled}."""
        display = _lora_display_name(lora_dict)
        self._name_label.setText(display)
        tooltip = _lora_tooltip(lora_dict)
        self.setToolTip(tooltip)
        self._enabled.setToolTip(tooltip)
        self._name_label.setToolTip(tooltip)
        self._strength.setToolTip(tooltip)
        with SignalBlocker(self._strength):
            self._strength.setValue(int(lora_dict.get("strength", 1.0) * 100))
        with SignalBlocker(self._enabled):
            self._enabled.is_checked = lora_dict.get("enabled", True)

    def to_dict(self, original: dict) -> dict:
        """Return updated LoRA dict preserving metadata fields."""
        result = dict(original)
        result["name"] = original.get("name", "")
        result["strength"] = self._strength.value() / 100.0
        result["enabled"] = self._enabled.isChecked()
        return result


class VerticalResizeHandle(QToolButton):
    drag_started = pyqtSignal(int)
    drag_moved = pyqtSignal(int)
    drag_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dragging = False
        self.setText("...")
        self.setAutoRaise(True)
        self.setFixedHeight(8)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip(_("Drag to resize"))

    def mousePressEvent(self, event: QMouseEvent | None):
        if event and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.drag_started.emit(self.mapToGlobal(event.pos()).y())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent | None):
        if self._dragging and event:
            self.drag_moved.emit(self.mapToGlobal(event.pos()).y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent | None):
        if self._dragging:
            self._dragging = False
            self.drag_finished.emit()
            if event:
                event.accept()
            return
        super().mouseReleaseEvent(event)


def _lora_display_name(lora: dict | str) -> str:
    """Extract a short display name from a LoRA file ID."""
    if isinstance(lora, dict):
        if display_name := str(lora.get("display_name", "")).strip():
            return display_name
        lora_id = str(lora.get("name", ""))
    else:
        lora_id = lora
    name = lora_id.replace("\\", "/")
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot > 0:
        name = name[:dot]
    return name


def _lora_tooltip(lora: dict) -> str:
    lora_id = str(lora.get("name", ""))
    display = _lora_display_name(lora)
    description = str(lora.get("description", "")).strip()
    lines = [_wrap_tooltip_text(display)]
    if lora_id and lora_id != display:
        lines.append(_wrap_tooltip_text(lora_id))
    if description:
        lines.extend(["", _wrap_tooltip_text(description)])
    return "\n".join(lines)


def _wrap_tooltip_text(text: str, width=56) -> str:
    result = []
    for line in text.splitlines():
        if line.strip():
            result.append(fill(line, width=width))
        else:
            result.append("")
    return "\n".join(result)


class LoraDockerPanel(QWidget):
    """Compact LoRA panel for the main docker showing active style's LoRAs."""

    _add_preset_data = "__add_lora_preset__"
    _model: Model | None = None
    _lora_items: list[LoraDockerItem]
    _columns = 2
    _item_height = 26
    _min_visible_rows = 1
    _max_visible_rows = 50

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lora_items = []
        self._updating = False
        self._visible_rows = self._clamp_visible_rows(settings.lora_docker_visible_rows)
        self._resize_start_y = 0
        self._resize_start_rows = self._visible_rows
        self._last_preset_name = ""

        self._layout = QVBoxLayout()
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self.setLayout(self._layout)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(3)
        header = QLabel("<b>" + _("LoRAs") + "</b>", self)
        header_layout.addWidget(header)

        self._preset_combo = NoWheelComboBox(self)
        self._preset_combo.setToolTip(_("Apply a saved LoRA preset for this style"))
        self._preset_combo.setMinimumWidth(90)
        self._preset_combo.currentIndexChanged.connect(self._apply_lora_preset)
        header_layout.addWidget(self._preset_combo, 1)

        self._save_preset_button = QToolButton(self)
        self._save_preset_button.setIcon(theme.icon("save"))
        self._save_preset_button.setToolTip(_("Update selected LoRA preset"))
        self._save_preset_button.clicked.connect(self._update_lora_preset)
        header_layout.addWidget(self._save_preset_button)

        self._delete_preset_button = QToolButton(self)
        self._delete_preset_button.setIcon(theme.icon("trash"))
        self._delete_preset_button.setToolTip(_("Delete selected LoRA preset"))
        self._delete_preset_button.clicked.connect(self._delete_lora_preset)
        header_layout.addWidget(self._delete_preset_button)

        self._layout.addLayout(header_layout)

        # Scrollable container for LoRA items
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll_area.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        self._scroll_content = QWidget()
        self._item_container = QGridLayout(self._scroll_content)
        self._item_container.setContentsMargins(0, 0, 0, 0)
        self._item_container.setSpacing(1)
        self._scroll_area.setWidget(self._scroll_content)
        self._layout.addWidget(self._scroll_area)

        self._resize_handle = VerticalResizeHandle(self)
        self._resize_handle.drag_started.connect(self._start_resize)
        self._resize_handle.drag_moved.connect(self._resize_to)
        self._resize_handle.drag_finished.connect(self._finish_resize)
        self._layout.addWidget(self._resize_handle)

        self._empty_label = QLabel(
            "<i>" + _("No LoRAs in active style") + "</i>", self
        )
        self._empty_label.setStyleSheet(f"color: {theme.grey};")
        self._layout.addWidget(self._empty_label)

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        if self._model is not None:
            self._model.effective_style_changed.disconnect(self._rebuild)
        self._model = model
        model.effective_style_changed.connect(self._rebuild)
        self._rebuild()

    def _rebuild(self):
        """Rebuild the LoRA item list from the active style."""
        if self._model is None or self._updating:
            return
        style = self._model.active_style
        loras = style.loras
        self._rebuild_preset_combo()

        # Hide all existing items first
        for item in self._lora_items:
            item.setVisible(False)

        # Show or create items for each LoRA
        for i, lora_dict in enumerate(loras):
            if i < len(self._lora_items):
                item = self._lora_items[i]
            else:
                item = LoraDockerItem(self)
                item.changed.connect(self._write_back)
                self._lora_items.append(item)
            row = i // self._columns
            column = i % self._columns
            self._item_container.addWidget(item, row, column)
            item.set_lora(lora_dict)
            item.setVisible(True)

        has_loras = len(loras) > 0
        self._empty_label.setVisible(not has_loras)
        self._scroll_area.setVisible(has_loras)
        self._resize_handle.setVisible(has_loras)

        if has_loras:
            self._update_scroll_height(len(loras))

    def _clamp_visible_rows(self, rows: int):
        return max(self._min_visible_rows, min(self._max_visible_rows, rows))

    def _update_scroll_height(self, lora_count: int | None = None):
        if lora_count is None and self._model is not None:
            lora_count = len(self._model.active_style.loras)
        if not lora_count:
            return
        row_count = (lora_count + self._columns - 1) // self._columns
        visible_count = min(row_count, self._visible_rows)
        self._scroll_area.setFixedHeight(self._item_height * visible_count + 4)
        self._scroll_content.setMinimumHeight(self._item_height * row_count)

    def _start_resize(self, global_y: int):
        self._resize_start_y = global_y
        self._resize_start_rows = self._visible_rows

    def _resize_to(self, global_y: int):
        row_delta = round((global_y - self._resize_start_y) / self._item_height)
        self._visible_rows = self._clamp_visible_rows(self._resize_start_rows + row_delta)
        self._update_scroll_height()

    def _finish_resize(self):
        if settings.lora_docker_visible_rows != self._visible_rows:
            settings.lora_docker_visible_rows = self._visible_rows
            settings.save()

    def _write_back(self):
        """Write modified strength/enabled values back to the style."""
        if self._model is None:
            return
        self._updating = True
        try:
            style = self._model.active_style
            new_loras = []
            for i, original in enumerate(style.loras):
                if i < len(self._lora_items) and self._lora_items[i].isVisible():
                    new_loras.append(self._lora_items[i].to_dict(original))
                else:
                    new_loras.append(original)
            style.loras = new_loras
            style.save()
        finally:
            self._updating = False
        self._refresh_preset_dirty_state()

    def _rebuild_preset_combo(self, selected: str | None = None):
        if self._model is None:
            return
        if selected is None:
            selected = self._last_preset_name
        style = self._model.active_style
        presets = style.lora_presets if isinstance(style.lora_presets, dict) else {}
        with SignalBlocker(self._preset_combo):
            self._preset_combo.clear()
            self._preset_combo.addItem(_("LoRA Preset"), "")
            for name in sorted(presets, key=lambda text: text.lower()):
                self._preset_combo.addItem(name, name)
            if presets:
                self._preset_combo.insertSeparator(self._preset_combo.count())
            self._preset_combo.addItem(_("Add new preset..."), self._add_preset_data)
            index = self._preset_combo.findData(selected)
            self._preset_combo.setCurrentIndex(index if index >= 0 else 0)
        self._last_preset_name = self._current_preset_name()
        self._refresh_preset_dirty_state()

    def _snapshot_loras(self):
        if self._model is None:
            return []
        return [dict(lora) for lora in self._model.active_style.loras]

    def _merge_lora_preset(self, preset: object):
        current = self._snapshot_loras()
        if not isinstance(preset, list):
            return current
        preset_by_name = {
            str(lora.get("name", "")): dict(lora)
            for lora in preset
            if isinstance(lora, dict) and lora.get("name")
        }
        merged = []
        for current_lora in current:
            name = str(current_lora.get("name", ""))
            if saved := preset_by_name.get(name):
                item = dict(current_lora)
                item.update(saved)
                item["name"] = name
                merged.append(item)
            else:
                merged.append(current_lora)
        return merged

    def _normalize_lora_snapshot(self, loras: object):
        if not isinstance(loras, list):
            return []
        result = []
        for lora in loras:
            if not isinstance(lora, dict):
                continue
            item = dict(lora)
            if "strength" in item:
                try:
                    item["strength"] = round(float(item["strength"]), 4)
                except (TypeError, ValueError):
                    pass
            result.append(item)
        return result

    def _current_preset_name(self):
        data = self._preset_combo.currentData()
        return data if isinstance(data, str) and data not in ["", self._add_preset_data] else ""

    def _has_lora_preset_changes(self, name: str):
        if self._model is None or not name:
            return False
        style = self._model.active_style
        presets = style.lora_presets if isinstance(style.lora_presets, dict) else {}
        preset = presets.get(name)
        return self._normalize_lora_snapshot(preset) != self._normalize_lora_snapshot(style.loras)

    def _refresh_preset_dirty_state(self):
        if self._model is None:
            return
        name = self._current_preset_name()
        dirty = self._has_lora_preset_changes(name)
        with SignalBlocker(self._preset_combo):
            for index in range(self._preset_combo.count()):
                data = self._preset_combo.itemData(index)
                if not isinstance(data, str) or data in ["", self._add_preset_data]:
                    continue
                text = f"{data} *" if data == name and dirty else data
                if self._preset_combo.itemText(index) != text:
                    self._preset_combo.setItemText(index, text)
        self._save_preset_button.setEnabled(bool(name) and dirty)
        self._delete_preset_button.setEnabled(bool(name))
        self._save_preset_button.setToolTip(
            _("Update selected LoRA preset")
            if dirty
            else _("No changes to save for the selected LoRA preset")
        )

    def _apply_lora_preset(self):
        if self._model is None or self._updating:
            return
        data = self._preset_combo.currentData()
        if data == self._add_preset_data:
            if not self._add_lora_preset():
                self._rebuild_preset_combo(self._last_preset_name)
            return
        name = self._current_preset_name()
        self._last_preset_name = name
        self._refresh_preset_dirty_state()
        if not name:
            return
        style = self._model.active_style
        presets = style.lora_presets if isinstance(style.lora_presets, dict) else {}
        preset = presets.get(name)
        if not isinstance(preset, list):
            return
        style.loras = self._merge_lora_preset(preset)
        style.save()
        self._rebuild()
        self._rebuild_preset_combo(name)

    def _add_lora_preset(self):
        if self._model is None:
            return False
        name, ok = QInputDialog.getText(
            self,
            _("Save LoRA Preset"),
            _("Preset name:"),
        )
        name = name.strip()
        if not ok or not name:
            return False
        style = self._model.active_style
        presets = dict(style.lora_presets) if isinstance(style.lora_presets, dict) else {}
        if name in presets:
            reply = QMessageBox.question(
                self,
                _("Overwrite LoRA Preset"),
                _("A LoRA preset with this name already exists. Overwrite it?"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return False
        presets[name] = self._snapshot_loras()
        style.lora_presets = presets
        style.save()
        self._last_preset_name = name
        self._rebuild_preset_combo(name)
        return True

    def _update_lora_preset(self):
        if self._model is None:
            return
        name = self._current_preset_name()
        if not name:
            return
        style = self._model.active_style
        presets = dict(style.lora_presets) if isinstance(style.lora_presets, dict) else {}
        if name not in presets:
            return
        presets[name] = self._snapshot_loras()
        style.lora_presets = presets
        style.save()
        self._last_preset_name = name
        self._rebuild_preset_combo(name)

    def _delete_lora_preset(self):
        if self._model is None:
            return
        name = self._current_preset_name()
        if not name:
            return
        style = self._model.active_style
        presets = dict(style.lora_presets) if isinstance(style.lora_presets, dict) else {}
        if name in presets:
            del presets[name]
            style.lora_presets = presets
            style.save()
        self._last_preset_name = ""
        self._rebuild_preset_combo()


class QuickStyleBar(QWidget):
    """Row of one-click generate buttons, one per configured quick style."""

    _model: Model | None = None
    _buttons: list[QPushButton]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons = []

        self._layout = QHBoxLayout()
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self.setLayout(self._layout)

        self._configure_button = QToolButton(self)
        self._configure_button.setText("⚙")
        self._configure_button.setToolTip(_("Configure quick generate styles"))
        self._configure_button.setFixedSize(QSize(22, 22))
        self._configure_button.clicked.connect(self._open_config_menu)
        self._layout.addWidget(self._configure_button)

        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(False)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._scroll_area.setFixedHeight(28)

        self._button_content = QWidget()
        self._button_layout = QHBoxLayout(self._button_content)
        self._button_layout.setContentsMargins(0, 0, 0, 0)
        self._button_layout.setSpacing(2)
        self._scroll_area.setWidget(self._button_content)
        self._layout.addWidget(self._scroll_area, 1)

        settings.changed.connect(self._handle_settings_changed)
        Styles.list().changed.connect(self._rebuild)
        Styles.list().name_changed.connect(self._rebuild)

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        self._model = model
        self._rebuild()

    def _handle_settings_changed(self, name: str, _value: object):
        if name == "quick_styles":
            self._rebuild()

    def _rebuild(self, *args):
        """Rebuild buttons from the quick_styles setting."""
        # Remove old buttons
        for btn in self._buttons:
            self._button_layout.removeWidget(btn)
            btn.deleteLater()
        self._buttons.clear()

        styles_list = Styles.list()
        for filename in settings.quick_styles:
            style = styles_list.find(filename)
            if style is None:
                continue
            btn = QPushButton(style.name, self._button_content)
            btn.setToolTip(_("Generate with") + f" {style.name}")
            btn.setMaximumHeight(22)
            btn.setMinimumWidth(btn.sizeHint().width())
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda checked=False, s=style: self._generate(s))
            self._button_layout.addWidget(btn)
            self._buttons.append(btn)
        self._resize_button_content()

    def _resize_button_content(self):
        if not hasattr(self, "_button_layout"):
            return
        margins = self._button_layout.contentsMargins()
        spacing = self._button_layout.spacing()
        button_width = sum(button.sizeHint().width() for button in self._buttons)
        button_height = max((button.sizeHint().height() for button in self._buttons), default=22)
        desired_width = (
            margins.left()
            + margins.right()
            + button_width
            + spacing * max(0, len(self._buttons) - 1)
        )
        viewport_width = max(1, self._scroll_area.viewport().width())
        needs_scroll = desired_width > viewport_width
        scroll_height = self._scroll_area.horizontalScrollBar().sizeHint().height()
        content_height = max(button_height, 22)
        area_height = content_height + (scroll_height if needs_scroll else 0) + 2
        content_width = max(viewport_width, desired_width)
        self._scroll_area.setFixedHeight(area_height)
        self._button_content.setFixedSize(content_width, content_height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_button_content()

    def _generate(self, style: Style):
        if self._model is not None:
            self._model.generate_with_style(style)

    def _open_config_menu(self):
        menu = QMenu(self)
        styles_list = Styles.list()
        current = set(settings.quick_styles)

        for style in styles_list.filtered():
            action = QAction(style.name, menu)
            action.setCheckable(True)
            action.setChecked(style.filename in current)
            action.toggled.connect(lambda checked, fn=style.filename: self._toggle_style(fn, checked))
            menu.addAction(action)

        menu.exec_(self._configure_button.mapToGlobal(
            QPoint(0, self._configure_button.height())
        ))

    def _toggle_style(self, filename: str, checked: bool):
        current = list(settings.quick_styles)
        if checked and filename not in current:
            current.append(filename)
        elif not checked and filename in current:
            current.remove(filename)
        settings.quick_styles = current
        settings.save()


class VramWidget(QWidget):
    """Compact VRAM usage indicator with a Free VRAM button."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.setLayout(layout)

        self._label = QLabel(_("VRAM") + ": --", self)
        self._label.setStyleSheet(f"color: {theme.grey}; font-size: 11px;")

        self._free_button = QPushButton(_("Free VRAM"), self)
        self._free_button.setMaximumHeight(20)
        self._free_button.setToolTip(_("Unload all models and free GPU memory"))
        self._free_button.clicked.connect(self._free_memory)

        self._restart_button = QPushButton(_("Restart Comfy"), self)
        self._restart_button.setMaximumHeight(20)
        self._restart_button.setToolTip(
            _("Cancel queued jobs, restart the managed ComfyUI server, and reconnect")
        )
        self._restart_button.clicked.connect(self._restart_comfy)

        layout.addWidget(self._label, 1)
        layout.addWidget(self._free_button)
        layout.addWidget(self._restart_button)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_stats)
        self._poll_timer.start(5000)  # poll every 5 seconds

        root.connection.state_changed.connect(self._on_connection_changed)
        settings.changed.connect(self._handle_settings_changed)
        self._update_restart_button()

    def _on_connection_changed(self):
        if root.connection.client_if_connected:
            self._poll_stats()
        self._update_restart_button()

    def _handle_settings_changed(self, key: str, _value: object):
        if key == "server_mode":
            self._update_restart_button()

    def _update_restart_button(self):
        self._restart_button.setEnabled(settings.server_mode is ServerMode.managed)

    def _poll_stats(self):
        client = root.connection.client_if_connected
        if client is None:
            self._label.setText(_("VRAM") + ": --")
            return
        eventloop.run(self._fetch_stats(client))

    async def _fetch_stats(self, client):
        try:
            data = await client.get_system_stats()
            devices = data.get("devices", [])
            if devices:
                dev = devices[0]
                total = dev.get("vram_total", 0)
                free = dev.get("vram_free", 0)
                used = total - free
                total_gb = total / (1024**3)
                used_gb = used / (1024**3)
                self._label.setText(
                    _("VRAM") + f": {used_gb:.1f} / {total_gb:.1f} GB"
                )
        except Exception:
            self._label.setText(_("VRAM") + ": --")

    def _free_memory(self):
        client = root.connection.client_if_connected
        if client is None:
            return
        eventloop.run(self._do_free(client))

    async def _do_free(self, client):
        try:
            await client.free_memory()
            # Refresh stats after freeing
            await self._fetch_stats(client)
        except Exception:
            pass

    def _restart_comfy(self):
        if settings.server_mode is not ServerMode.managed:
            return
        self._free_button.setEnabled(False)
        self._restart_button.setEnabled(False)
        self._label.setText(_("VRAM") + ": " + _("restarting..."))
        eventloop.run(self._do_restart_comfy())

    def _cancel_local_jobs(self):
        for model in root.models:
            for job in list(model.jobs):
                if job.state in [JobState.queued, JobState.executing]:
                    model.jobs.notify_cancelled(job)

    async def _do_restart_comfy(self):
        try:
            self._cancel_local_jobs()
            if root.connection.state is not ConnectionState.disconnected:
                await root.connection.disconnect()
            if root.server.state is not ServerState.stopped:
                await root.server.force_stop()
            url = await root.server.start()
            await root.connection._connect(url, ServerMode.managed)
            if client := root.connection.client_if_connected:
                await self._fetch_stats(client)
        except Exception:
            self._label.setText(_("VRAM") + ": " + _("restart failed"))
        finally:
            self._free_button.setEnabled(True)
            self._update_restart_button()


class LayerCountWidget(QWidget):
    value_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 4

        self._layout = QHBoxLayout()
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self._layout)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setMinimum(1)
        self._slider.setMaximum(10)
        self._slider.setValue(self._value)
        self._slider.setSingleStep(1)
        self._slider.valueChanged.connect(self._notify_changed)

        self._input = QSpinBox(self)
        self._input.setPrefix(_("Layers") + ": ")
        self._input.setMinimum(1)
        self._input.setMaximum(10)
        self._input.setValue(self._value)
        self._input.valueChanged.connect(self._notify_changed)

        self._layout.addWidget(self._slider)
        self._layout.addWidget(self._input)

    def _notify_changed(self, value: int):
        if value != self._value:
            self._value = value
            self._update()
            self.value_changed.emit(self._value)

    def _update(self):
        with SignalBlocker(self._slider), SignalBlocker(self._input):
            self._slider.setValue(self._value)
            self._input.setValue(self._value)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value: int):
        if value == self._value:
            return
        self._value = value
        self._update()


class WorkspaceSelectWidget(QToolButton):
    _icons: ClassVar[dict[Workspace, QIcon]] = {
        Workspace.generation: theme.icon("workspace-generation"),
        Workspace.upscaling: theme.icon("workspace-upscaling"),
        Workspace.live: theme.icon("workspace-live"),
        Workspace.animation: theme.icon("workspace-animation"),
        Workspace.custom: theme.icon("workspace-custom"),
    }

    _value = Workspace.generation

    def __init__(self, parent):
        super().__init__(parent)

        menu = QMenu(self)
        menu.addAction(self._create_action(_("Generate"), Workspace.generation))
        menu.addAction(self._create_action(_("Upscale"), Workspace.upscaling))
        menu.addAction(self._create_action(_("Live"), Workspace.live))
        menu.addAction(self._create_action(_("Animation"), Workspace.animation))
        menu.addAction(self._create_action(_("Graph"), Workspace.custom))

        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setMenu(menu)
        self.setPopupMode(QToolButton.InstantPopup)
        self.setToolTip(
            _("Switch between workspaces: image generation, upscaling, live preview and animation.")
        )
        self.setMinimumWidth(int(self.sizeHint().width() * 1.6))
        self.value = Workspace.generation

    def paintEvent(self, a0):
        _paint_tool_drop_down(self)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, workspace: Workspace):
        self._value = workspace
        self.setIcon(self._icons[workspace])

    def _create_action(self, name: str, workspace: Workspace):
        action = QAction(name, self)
        action.setIcon(self._icons[workspace])
        action.setIconVisibleInMenu(True)
        action.triggered.connect(actions.set_workspace(workspace))
        return action


def _get_width_dip(s: QPaintDevice):
    # get device-indpendent width for things like QPixmap, which report their size in physical pixels
    if ratio := s.devicePixelRatioF():
        return int(s.width() / ratio)
    return s.width()


class GenerateButton(QPushButton):
    ctrl_clicked = pyqtSignal()

    def __init__(self, kind: JobKind, parent: QWidget):
        super().__init__(parent)
        self.model = root.active_model
        self._operation = _("Generate")
        self._kind = kind
        self._cost = 0
        self._cost_icon = theme.icon("interstice")
        self._seed_icon = theme.icon("seed")
        self._resolution_icon = theme.icon("resolution-multiplier")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    @property
    def operation(self):
        return self._operation

    @operation.setter
    def operation(self, value: str):
        self._operation = value
        self.update()

    def minimumSizeHint(self):
        fm = self.fontMetrics()
        return QSize(fm.width(self._operation) + 40, 12 + int(1.3 * fm.height()))

    def enterEvent(self, a0: QEvent | None):
        if (client := root.connection.client_if_connected) and client.user:
            self._cost = self.model.estimate_cost(self._kind)

    def leaveEvent(self, a0: QEvent | None):
        self._cost = 0

    def mouseReleaseEvent(self, e: QMouseEvent | None):
        if (
            e
            and e.button() == Qt.MouseButton.LeftButton
            and bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        ):
            self.ctrl_clicked.emit()
            e.accept()
            self.setDown(False)
        else:
            super().mouseReleaseEvent(e)

    def paintEvent(self, a0: QPaintEvent | None) -> None:
        opt = QStyleOption()
        opt.initFrom(self)
        opt.state |= QStyle.StateFlag.State_Sunken if self.isDown() else 0
        painter = QPainter(self)
        fm = self.fontMetrics()
        style = ensure(self.style())
        align = (
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
            | Qt.AlignmentFlag.AlignAbsolute
        )
        rect = self.rect()
        pixmap = self.icon().pixmap(int(fm.height() * 1.3))
        pixmap_width = _get_width_dip(pixmap)
        is_hover = int(opt.state) & QStyle.StateFlag.State_MouseOver
        element = QStyle.PrimitiveElement.PE_PanelButtonCommand
        content_width = fm.width(self._operation) + 5 + pixmap_width
        content_rect = rect.adjusted(int(0.5 * (rect.width() - content_width)), 0, 0, 0)
        style.drawPrimitive(element, opt, painter, self)
        style.drawItemPixmap(painter, content_rect, align, pixmap)
        content_rect = content_rect.adjusted(pixmap_width + 5, 0, 0, 0)
        style.drawItemText(painter, content_rect, align, self.palette(), True, self._operation)

        cost_width = 0
        if is_hover and self._cost > 0:
            pixmap = self._cost_icon.pixmap(fm.height())
            text_width = fm.width(str(self._cost))
            cost_width = text_width + 16 + pixmap_width
            cost_rect = rect.adjusted(rect.width() - cost_width, 0, 0, 0)
            painter.setOpacity(0.3)
            painter.drawLine(
                cost_rect.left(), cost_rect.top() + 6, cost_rect.left(), cost_rect.bottom() - 6
            )
            painter.setOpacity(0.7)
            cost_rect = cost_rect.adjusted(6, 0, 0, 0)
            style.drawItemText(painter, cost_rect, align, self.palette(), True, str(self._cost))
            cost_rect = cost_rect.adjusted(text_width + 4, 0, 0, 0)
            style.drawItemPixmap(painter, cost_rect, align, pixmap)

        seed_width = 0
        if is_hover and self.model.fixed_seed:
            pixmap = self._seed_icon.pixmap(fm.height())
            seed_width = pixmap_width + 4
            seed_rect = rect.adjusted(rect.width() - cost_width - seed_width, 0, 0, 0)
            style.drawItemPixmap(painter, seed_rect, align, pixmap)

        if is_hover and self.model.resolution_multiplier != 1.0:
            pixmap = self._resolution_icon.pixmap(fm.height())
            resolution_rect = rect.adjusted(
                rect.width() - cost_width - seed_width - pixmap_width - 4, 0, 0, 0
            )
            style.drawItemPixmap(painter, resolution_rect, align, pixmap)


class ErrorBox(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._error = no_error
        self._original_error = ""

        self.setObjectName("errorBox")
        self.setFrameStyle(QFrame.Shape.StyledPanel)

        self._label = QLabel(self)
        self._label.setWordWrap(True)
        self._label.setOpenExternalLinks(True)
        self._label.setTextFormat(Qt.TextFormat.RichText)

        self._copy_button = QToolButton(self)
        self._copy_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._copy_button.setIcon(Krita.instance().icon("edit-copy"))
        self._copy_button.setToolTip(_("Copy error message to clipboard"))
        self._copy_button.setAutoRaise(True)
        self._copy_button.clicked.connect(self._copy_error)

        self._recharge_button = QToolButton(self)
        self._recharge_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._recharge_button.setText(_("Charge"))
        self._recharge_button.setIcon(theme.icon("interstice"))
        self._recharge_button.clicked.connect(self._recharge)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._label)
        layout.addWidget(self._copy_button)
        layout.addWidget(self._recharge_button)

        self.reset()

    def reset(self, color: str = theme.red):
        self._copy_button.setVisible(False)
        self._recharge_button.setVisible(False)
        self._label.setStyleSheet(f"color: {color};")
        if color == theme.red:
            self.setStyleSheet("QFrame#errorBox { border: 1px solid #a01020; }")
        else:
            self.setStyleSheet(None)
        self.setVisible(False)

    @property
    def error(self):
        return self._error

    @error.setter
    def error(self, error: Error):
        self.reset()
        self._error = error
        self._original_error = error.message if error else ""
        if error.kind is ErrorKind.insufficient_funds:
            self._show_payment_error(error.data)
        elif error.kind.is_warning:
            self._show_warning(error.kind, error.message)
        elif error:
            self._show_error(error.message)

    def _show_error(self, text: str):
        server_error_prefix = _("Server error")
        if text.startswith(f"{server_error_prefix}: Validation error:"):
            text = text[len(server_error_prefix) + 1 :]
        if text.count("\n") > 3:
            lines = text.split("\n")
            n = 1
            text = lines[-n]
            while n < len(lines) and text.strip() == "":
                n += 1
                text = lines[-n]
        if len(text) > 60 * 3:
            text = text[: 60 * 2] + " [...] " + text[-60:]
        self._label.setText(text)
        if text != self._original_error:
            self._label.setToolTip(self._original_error)
        self._copy_button.setVisible(True)
        self.setVisible(True)

    def _show_warning(self, kind: ErrorKind, text: str):
        self.reset(theme.yellow)
        if kind is ErrorKind.incompatible_lora:
            text = (
                _(
                    "Selected LoRA model could not be applied. Please make sure it is compatible with the checkpoint base model you are using."
                )
                + " <a href='https://docs.interstice.cloud/base-models'>"
                + _("Learn more")
                + "</a>"
            )
        self._label.setText(text)
        self.setVisible(True)

    def _show_payment_error(self, data: dict[str, Any] | None):
        self.reset(theme.yellow)
        message = "Insufficient funds"
        if data:
            message = _(
                "Insufficient funds - generation would cost {cost} tokens. Remaining tokens: {tokens}",
                cost=data["cost"],
                tokens=data["credits"],
            )
        self._label.setText(message)
        self._recharge_button.setVisible(True)
        self.setVisible(True)

    def _copy_error(self):
        if clipboard := QGuiApplication.clipboard():
            clipboard.setText(self._original_error)

    def _recharge(self):
        QDesktopServices.openUrl(QUrl("https://www.interstice.cloud/user"))


def create_wide_tool_button(icon_name: str, text: str, parent=None):
    button = QToolButton(parent)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    button.setIcon(theme.icon(icon_name))
    button.setToolTip(text)
    button.setAutoRaise(True)
    icon_height = button.iconSize().height()
    button.setIconSize(QSize(int(icon_height * 1.25), icon_height))
    return button


def create_framed_label(text: str, parent=None):
    frame = QFrame(parent)
    frame.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Plain)
    label = QLabel(parent=frame)
    label.setText(text)
    frame_layout = QHBoxLayout()
    frame_layout.setContentsMargins(4, 2, 4, 2)
    frame_layout.addWidget(label)
    frame.setLayout(frame_layout)
    return frame, label


def _paint_tool_drop_down(widget: QToolButton, text: str | None = None):
    opt = QStyleOption()
    opt.initFrom(widget)
    painter = QPainter(widget)
    style = ensure(widget.style())
    align = (
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignAbsolute
    )
    rect = widget.rect()
    pixmap = widget.icon().pixmap(int(rect.height() * 0.75))
    element = QStyle.PrimitiveElement.PE_Widget
    if int(opt.state) & QStyle.StateFlag.State_MouseOver:
        element = QStyle.PrimitiveElement.PE_PanelButtonCommand
    style.drawPrimitive(element, opt, painter, widget)
    style.drawItemPixmap(painter, rect.adjusted(4, 0, 0, 0), align, pixmap)
    if text:
        text_rect = rect.adjusted(_get_width_dip(pixmap) + 4, 0, 0, 0)
        style.drawItemText(painter, text_rect, align, widget.palette(), True, text)
    painter.translate(int(0.5 * rect.width() - 10), 0)
    style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorArrowDown, opt, painter)
