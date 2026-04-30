import re
from pathlib import Path

from PyQt5.QtCore import QByteArray
from PyQt5.QtGui import QGuiApplication, QImage

from ..image import Bounds, Image
from ..localization import translate as _
from ..model import Workspace
from ..root import root


_image_mime_types = ("image/png", "image/jpeg", "image/webp", "image/bmp")
_image_file_extensions = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
}


def _clipboard_mime_data():
    clipboard = QGuiApplication.clipboard()
    return clipboard.mimeData() if clipboard else None


def clipboard_may_contain_image():
    mime = _clipboard_mime_data()
    if mime is None:
        return False
    if mime.hasImage():
        return True
    if any(mime.data(mime_type).size() > 0 for mime_type in _image_mime_types):
        return True
    if mime.hasUrls():
        for url in mime.urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.exists() and path.suffix.lower() in _image_file_extensions:
                    return True
            if url.scheme() == "blob":
                return True
    return any(
        re.search(r"data:image/[-+.\w]+;base64,", text) or "blob:" in text
        for text in (mime.html(), mime.text())
    )


def _image_from_clipboard_data() -> Image | None:
    mime = _clipboard_mime_data()
    if mime is None:
        return None

    if mime.hasImage():
        data = mime.imageData()
        if isinstance(data, QImage) and not data.isNull():
            return Image(data.copy())
        if hasattr(data, "toImage"):
            qimage = data.toImage()
            if isinstance(qimage, QImage) and not qimage.isNull():
                return Image(qimage.copy())

    for mime_type in _image_mime_types:
        data = mime.data(mime_type)
        if data and data.size() > 0:
            try:
                return Image.from_bytes(data)
            except RuntimeError:
                pass

    if mime.hasUrls():
        for url in mime.urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.exists() and path.suffix.lower() in _image_file_extensions:
                    try:
                        return Image.load(path)
                    except RuntimeError:
                        pass

    for text in [mime.html(), mime.text()]:
        match = re.search(r"data:image/[-+.\w]+;base64,([A-Za-z0-9+/=\s]+)", text)
        if match:
            data = QByteArray.fromBase64(match[1].encode("ascii"))
            try:
                return Image.from_bytes(data)
            except RuntimeError:
                pass

    return None


def paste_clipboard_image(*_args, report_error=True):
    if model := root.model_for_active_document():
        image = _image_from_clipboard_data()
        if image is None:
            if report_error:
                model.report_error(
                    _(
                        "Clipboard does not contain image data. If the source copied a browser "
                        "blob URL, use Copy Image or save/download the image first."
                    )
                )
            return False
        bounds = Bounds(0, 0, image.width, image.height)
        model.layers.create(_("Pasted Clipboard Image"), image, bounds)
        return True
    return False


def generate():
    if model := root.model_for_active_document():
        if model.workspace is Workspace.generation:
            model.generate()
        elif model.workspace is Workspace.upscaling:
            model.upscale_image()
        elif model.workspace is Workspace.live:
            model.generate_live()
        elif model.workspace is Workspace.animation:
            model.animation.generate()
        elif model.workspace is Workspace.custom:
            model.custom.generate()


def cancel_active():
    if model := root.model_for_active_document():
        model.cancel(active=True)


def cancel_queued():
    if model := root.model_for_active_document():
        model.cancel(queued=True)


def cancel_all():
    if model := root.model_for_active_document():
        model.cancel(active=True, queued=True)


def toggle_preview():
    if model := root.model_for_active_document():
        model.jobs.toggle_selection()


def apply():
    if model := root.model_for_active_document():
        if model.workspace is Workspace.generation and len(model.jobs.selection) > 0:
            model.apply_generated_result(*model.jobs.selection[0])
        elif model.workspace is Workspace.live:
            model.live.apply_result()


def apply_alternative():
    if model := root.model_for_active_document():
        if model.workspace is Workspace.live:
            model.live.apply_result(layer_only=True)
        else:
            apply()


def create_region():
    if model := root.model_for_active_document():
        model.regions.create_region(group=model.workspace is not Workspace.live)


def set_workspace(workspace: Workspace):
    def action():
        if model := root.model_for_active_document():
            model.workspace = workspace

    return action


def toggle_workspace():
    if model := root.model_for_active_document():
        l = list(Workspace)
        next = l[(l.index(model.workspace) + 1) % len(l)]
        model.workspace = next


def toggle_edit_mode():
    if model := root.model_for_active_document():
        model.edit_mode = not model.edit_mode
