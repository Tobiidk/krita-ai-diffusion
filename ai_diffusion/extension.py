import sys
from collections.abc import Callable
from pathlib import Path

from krita import DockWidgetFactory, DockWidgetFactoryBase, Extension, Krita, Window  # type: ignore
from PyQt5.QtCore import QEvent, QTimer
from PyQt5.QtGui import QKeyEvent, QKeySequence
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
)

from . import __version__, eventloop
from .localization import translate as _
from .model import Workspace
from .root import root
from .settings import settings
from .ui import actions
from .ui.diffusion import ImageDiffusionWidget
from .ui.settings import SettingsDialog
from .util import client_logger as log


class AIToolsExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self._actions: dict[str, QAction] = {}
        self._event_filter_installed = False
        self._autostart_started = False

        log.info(f"Extension initialized, Version: {__version__}, Python: {sys.version}")

        extension_dir = Path(__file__).parent
        debugpy_path = extension_dir / "debugpy" / "src"
        if debugpy_path.exists():
            try:
                sys.path.insert(0, str(debugpy_path))
                import debugpy

                debugpy.listen(("127.0.0.1", 5678), in_process_debug_adapter=True)
                log.info("Developer mode: debugpy listening on port 5678")
            except ImportError:
                pass

        pykrita_dir = extension_dir.parent
        if pykrita_dir.name != "pykrita" and not (pykrita_dir / ".git").exists():
            log.warning(
                "Plugin is not installed in a 'pykrita' directory, this may break user files "
                f"and settings. Detected installation path is: {pykrita_dir}"
            )

        eventloop.setup()
        log.info("Loading plugin settings")
        settings.load()
        log.info("Initializing plugin root")
        root.init()
        log.info("Plugin root initialized")
        self._settings_dialog: SettingsDialog | None = None

        notifier = Krita.instance().notifier()
        notifier.setActive(True)
        notifier.applicationClosing.connect(self.shutdown)  # type: ignore
        if app := QApplication.instance():
            app.installEventFilter(self)
            self._event_filter_installed = True
            QTimer.singleShot(0, self._start_autostart_once)
        log.info("Extension constructor completed")

    def setup(self):
        log.info("Extension setup called")
        self._start_autostart_once()

    def _start_autostart_once(self):
        if self._autostart_started:
            return
        self._autostart_started = True
        log.info("Starting plugin autostart")
        eventloop.run(root.autostart(self._notify_server_settings_changed))

    def _notify_server_settings_changed(self):
        if self._settings_dialog is not None:
            self._settings_dialog.connection.update_ui()

    def _show_settings(self):
        if self._settings_dialog is None:
            try:
                log.info("Creating settings dialog")
                self._settings_dialog = SettingsDialog(root.server)
            except Exception as e:
                log.exception(f"Failed to create settings dialog: {e}")
                return
        self._settings_dialog.show()

    def shutdown(self):
        if self._event_filter_installed:
            if app := QApplication.instance():
                app.removeEventFilter(self)
            self._event_filter_installed = False
        root.server.terminate()
        eventloop.stop()

    def eventFilter(self, watched, event):
        if (
            event is not None
            and event.type() in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress)
            and isinstance(event, QKeyEvent)
            and event.matches(QKeySequence.StandardKey.Paste)
            and self._should_handle_image_paste()
        ):
            event.accept()
            if event.type() == QEvent.Type.KeyPress:
                actions.paste_clipboard_image()
            return True
        return super().eventFilter(watched, event)

    def _should_handle_image_paste(self):
        if QApplication.activeModalWidget() is not None:
            return False
        if root.model_for_active_document() is None:
            return False
        if self._focus_accepts_text():
            return False
        return actions.clipboard_may_contain_image()

    def _focus_accepts_text(self):
        widget = QApplication.focusWidget()
        if widget is None:
            return False
        if isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox)):
            return True
        parent = widget
        while parent is not None:
            if isinstance(parent, QComboBox) and parent.isEditable():
                return True
            parent = parent.parentWidget()
        return False

    def _create_action(
        self, window: Window, name: str, func: Callable[[], None], text="", menu=""
    ):
        action = window.createAction(f"ai_diffusion_{name}", text, menu)
        action.triggered.connect(func)
        self._actions[name] = action

    def createActions(self, window):
        log.info("Creating plugin actions")
        self._create_action(window, "settings", self._show_settings)
        self._create_action(window, "generate", actions.generate)
        self._create_action(window, "cancel", actions.cancel_active)
        self._create_action(window, "cancel_queued", actions.cancel_queued)
        self._create_action(window, "cancel_all", actions.cancel_all)
        self._create_action(window, "toggle_preview", actions.toggle_preview)
        self._create_action(window, "apply", actions.apply)
        self._create_action(window, "apply_alternative", actions.apply_alternative)
        self._create_action(window, "create_region", actions.create_region)
        self._create_action(
            window, "switch_workspace_generation", actions.set_workspace(Workspace.generation)
        )
        self._create_action(
            window, "switch_workspace_upscaling", actions.set_workspace(Workspace.upscaling)
        )
        self._create_action(window, "switch_workspace_live", actions.set_workspace(Workspace.live))
        self._create_action(
            window, "switch_workspace_graph", actions.set_workspace(Workspace.custom)
        )
        self._create_action(window, "toggle_workspace", actions.toggle_workspace)
        self._create_action(window, "toggle_edit_mode", actions.toggle_edit_mode)
        self._create_action(
            window,
            "paste_clipboard_image",
            actions.paste_clipboard_image,
            _("Paste Clipboard Image as Layer"),
            "tools/scripts",
        )


Krita.instance().addExtension(AIToolsExtension(Krita.instance()))
Krita.instance().addDockWidgetFactory(
    DockWidgetFactory("imageDiffusion", DockWidgetFactoryBase.DockRight, ImageDiffusionWidget)  # type: ignore
)
