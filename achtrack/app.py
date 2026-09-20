"""Qt application: worker thread, tray icon, single-instance handling."""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .config import APP_ICON, APP_ID, APP_NAME, Config
from .db import Database
from .engine import Engine

log = logging.getLogger(__name__)
SOCKET = f"{APP_ID}-{os.getuid()}"


class Worker(QThread):
    """Runs the engine off the UI thread; the engine calls the on_* methods, which become Qt signals."""

    unlocked = Signal(object)
    game_started = Signal(int, str, str)
    game_stopped = Signal(int, int)
    changed = Signal()
    error = Signal(str)

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.requests: queue.Queue = queue.Queue()
        self._stop = threading.Event()

    def run(self) -> None:
        try:
            Engine(self.cfg, Database(), listener=self, requests=self.requests).run(self._stop)
        except Exception:
            log.exception("engine crashed")

    def request(self, *req) -> None:
        self.requests.put(req)

    def stop(self) -> None:
        self._stop.set()
        self.wait(8000)

    def on_unlock(self, event: dict) -> None:
        self.unlocked.emit(event)

    def on_game_started(self, appid: int, name: str, header: str) -> None:
        self.game_started.emit(appid, name, header)

    def on_game_stopped(self, appid: int, seconds: int) -> None:
        self.game_stopped.emit(appid, seconds)

    def on_changed(self) -> None:
        self.changed.emit()

    def on_error(self, message: str) -> None:
        self.error.emit(message)


def send_to_running(message: str) -> bool:
    sock = QLocalSocket()
    sock.connectToServer(SOCKET)
    if not sock.waitForConnected(400):
        return False
    sock.write(message.encode())
    sock.flush()
    sock.waitForBytesWritten(400)
    sock.disconnectFromServer()
    return True


def prepare_environment(cfg: Config) -> None:
    # Native Wayland Qt windows can't choose their position or stay above games; XWayland can.
    # Desktops like COSMIC set QT_QPA_PLATFORM=wayland for every app, so override it here.
    if cfg.force_x11 and os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"


def make_app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(QIcon(str(APP_ICON)))
    app.setQuitOnLastWindowClosed(False)
    return app


def run_gui(background: bool = False) -> int:
    cfg = Config.load()
    prepare_environment(cfg)
    app = make_app()
    if send_to_running("background" if background else "show"):
        log.info("already running - handed over to the existing instance")
        return 0

    from .gui import MainWindow
    from .notifier import Notifier, ToastData

    tray = QSystemTrayIcon(QIcon(str(APP_ICON))) if QSystemTrayIcon.isSystemTrayAvailable() else None
    notifier = Notifier(cfg, tray)
    worker = Worker(cfg)
    win = MainWindow(cfg, worker, notifier, has_tray=tray is not None)

    def on_close_without_tray():
        notifier.toasts.show(ToastData(kicker=APP_NAME, title="Still running in the background",
                                       body=f"Run “{APP_ID}” again to open the library. Quit from Library → Quit.",
                                       sound=None))

    win.on_hidden_without_tray = on_close_without_tray

    if tray is not None:
        menu = QMenu()
        menu.addAction("Open library", win.bring_up)
        menu.addAction("Test popup", lambda: notifier.test(False))
        menu.addAction("Rescan for games", lambda: worker.request("rescan"))
        menu.addSeparator()
        menu.addAction("Quit", app.quit)
        tray.setContextMenu(menu)
        tray.setToolTip(APP_NAME)
        tray.activated.connect(lambda reason: win.bring_up()
                               if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        win._tray_menu = menu

    server = QLocalServer()
    QLocalServer.removeServer(SOCKET)
    server.listen(SOCKET)

    def handle(message: str) -> None:
        message = message.strip()
        if message == "show":
            win.bring_up()
        elif message in ("test", "test-rare"):
            notifier.test(message == "test-rare")
        elif message == "rescan":
            worker.request("rescan")
        elif message == "quit":
            app.quit()

    def on_connection() -> None:
        sock = server.nextPendingConnection()
        if sock is None:
            return
        sock.readyRead.connect(lambda: handle(bytes(sock.readAll()).decode(errors="replace")))
        sock.disconnected.connect(sock.deleteLater)

    server.newConnection.connect(on_connection)

    worker.unlocked.connect(notifier.achievement)
    worker.game_started.connect(notifier.game_started)
    worker.game_started.connect(win.game_started)
    worker.game_stopped.connect(win.game_stopped)
    worker.changed.connect(win.schedule_reload)
    worker.error.connect(lambda msg: win.statusBar().showMessage(msg, 15000))
    worker.start()

    if background:
        win.reload(force=True)
    else:
        win.show()
        QTimer.singleShot(0, lambda: win.reload(force=True))

    app.aboutToQuit.connect(worker.stop)
    code = app.exec()
    server.close()
    return code


def show_test_popup(rare: bool) -> int:
    cfg = Config.load()
    prepare_environment(cfg)
    app = make_app()
    if send_to_running("test-rare" if rare else "test"):
        return 0
    from .notifier import Notifier

    notifier = Notifier(cfg)
    notifier.test(rare)
    QTimer.singleShot(int((cfg.popup_seconds + 1.5) * 1000), app.quit)
    return app.exec()
