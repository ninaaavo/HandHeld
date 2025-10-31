import sys, os
from PySide6 import QtCore, QtGui, QtWidgets, QtMultimedia, QtMultimediaWidgets
import serial
import serial.tools.list_ports
TEST_MODE = False  # set to False when using ESP32

# ------------------- USER SETTINGS -------------------
PORT = "COM3"  # <-- put your ESP32 COM port here
BAUD = 115200

GENERIC_VIDEO = r"C:\Users\baong\Documents\HANDHELD\testVids\generic.mp4"
VIDEO_MAP = {
    "PLAY1": r"C:\Users\baong\Documents\HANDHELD\testVids\vid1.mp4",
    "PLAY2": r"C:\Users\baong\Documents\HANDHELD\testVids\vid2.mp4",
    "PLAY3": r"C:\Users\baong\Documents\HANDHELD\testVids\vid3.mp4",
    "PLAY4": r"C:\Users\baong\Documents\HANDHELD\testVids\vid4.mp4",

}
FADE_MS = 700          # fade duration (ms)
TARGET_VOLUME = 80      # % volume when fully on
# ------------------------------------------------------

def url(path: str) -> QtCore.QUrl:
    return QtCore.QUrl.fromLocalFile(os.path.abspath(path))

class SerialPoller(QtCore.QObject):
    line = QtCore.Signal(str)
    error = QtCore.Signal(str)

    def __init__(self, port, baud, parent=None):
        super().__init__(parent)
        self.port_name = port
        self.baud = baud
        self.ser = None
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(15)
        self.timer.timeout.connect(self.poll)
        self.buf = []

    def start(self):
        try:
            self.ser = serial.Serial(self.port_name, self.baud, timeout=0)
            self.timer.start()
        except Exception as e:
            self.error.emit(f"Serial open failed on {self.port_name}: {e}")

    def poll(self):
        try:
            data = self.ser.read(1024)
            if not data:
                return
            for b in data:
                ch = chr(b)
                if ch in ("\r", "\n"):
                    if self.buf:
                        msg = "".join(self.buf).strip().upper()
                        self.buf.clear()
                        if msg:
                            self.line.emit(msg)
                else:
                    self.buf.append(ch)
        except Exception as e:
            self.error.emit(f"Serial read error: {e}")
            self.timer.stop()

class VideoFader(QtWidgets.QWidget):
    """
    Uses QGraphicsView + QGraphicsVideoItem for true compositing:
      - Background (generic) ALWAYS playing & visible (opacity 1.0).
      - Foreground sits on top (higher Z) and fades its opacity in/out.
      - Audio crossfades between bg and fg.
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ESP32 Touch Video Fader")
        self.resize(1280, 720)

        self.fade_group = None

        # --- Scene/View setup ---
        self.view = QtWidgets.QGraphicsView(self)
        self.view.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.view.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        self.scene = QtWidgets.QGraphicsScene(self.view)
        self.view.setScene(self.scene)

        # --- Video items (composited) ---
        self.bg_item = QtMultimediaWidgets.QGraphicsVideoItem()
        self.fg_item = QtMultimediaWidgets.QGraphicsVideoItem()
        self.scene.addItem(self.bg_item)
        self.scene.addItem(self.fg_item)

        # Z-order: fg on top
        self.bg_item.setZValue(0)
        self.fg_item.setZValue(1)

        # Opacity: bg always visible; fg starts hidden
        self.bg_item.setOpacity(1.0)
        self.fg_item.setOpacity(0.0)

        # Fill (crop) behavior — change to KeepAspectRatio for letterboxing
        self.bg_item.setAspectRatioMode(QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        self.fg_item.setAspectRatioMode(QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding)

        # 🔹 Listen to the *viewport's* resize (fires after layout with true size)
        self.view.viewport().installEventFilter(self)
        # 🔹 After first show/layout pass, size everything once
        QtCore.QTimer.singleShot(0, lambda: self._apply_viewport_size(self.view.viewport().size()))

        # --- Players & Audio ---
        self.bg_player = QtMultimedia.QMediaPlayer(self)
        self.fg_player = QtMultimedia.QMediaPlayer(self)
        self.bg_player.setVideoOutput(self.bg_item)
        self.fg_player.setVideoOutput(self.fg_item)

        self.bg_audio = QtMultimedia.QAudioOutput(self)
        self.fg_audio = QtMultimedia.QAudioOutput(self)
        self.bg_player.setAudioOutput(self.bg_audio)
        self.fg_player.setAudioOutput(self.fg_audio)

        # Initial volumes
        self.bg_audio.setVolume(max(0.0, min(1.0, TARGET_VOLUME / 100.0)))
        self.fg_audio.setVolume(0.0)

        # --- Generic source + loop (always playing) ---
        if not os.path.exists(GENERIC_VIDEO):
            print(f"[ERROR] GENERIC_VIDEO not found: {GENERIC_VIDEO}")
        self.bg_player.setSource(url(GENERIC_VIDEO))
        try:
            self.bg_player.setLoops(QtMultimedia.QMediaPlayer.Infinite)
        except AttributeError:
            self.bg_player.mediaStatusChanged.connect(
                lambda s: self.bg_player.play()
                if s == QtMultimedia.QMediaPlayer.EndOfMedia else None
            )

        # Keep bg playing
        self.bg_player.mediaStatusChanged.connect(self._ensure_bg_playing)
        self.bg_player.playbackStateChanged.connect(self._ensure_bg_playing_state)

        # Foreground end/stop -> fade out + bg volume up
        self.fg_player.mediaStatusChanged.connect(self._on_fg_media_status)
        self.fg_player.playbackStateChanged.connect(self._on_fg_state_changed)

        # If/when metadata becomes available, refit to current viewport size
        self.bg_player.metaDataChanged.connect(lambda: self._apply_viewport_size(self.view.viewport().size()))
        self.fg_player.metaDataChanged.connect(lambda: self._apply_viewport_size(self.view.viewport().size()))

        # Start bg once event loop is live
        QtCore.QTimer.singleShot(0, self._start_bg)

    # ✅ Apply size to scene and both video items (called on viewport resize & first show)
    def _apply_viewport_size(self, qsize: QtCore.QSize):
        w, h = qsize.width(), qsize.height()
        self.scene.setSceneRect(0, 0, w, h)
        s = QtCore.QSizeF(w, h)
        self.bg_item.setSize(s)
        self.fg_item.setSize(s)

    # ✅ Catch viewport resize events — guarantees we get the real on-screen size
    def eventFilter(self, obj, event):
        if obj is self.view.viewport() and event.type() == QtCore.QEvent.Resize:
            self._apply_viewport_size(event.size())
        return super().eventFilter(obj, event)

    # --- BG helpers ---
    def _start_bg(self):
        self.bg_player.setPosition(0)
        self.bg_player.play()

    def _ensure_bg_playing(self, status):
        if status in (
            QtMultimedia.QMediaPlayer.MediaStatus.LoadedMedia,
            QtMultimedia.QMediaPlayer.MediaStatus.BufferedMedia,
            QtMultimedia.QMediaPlayer.MediaStatus.LoadingMedia,
        ):
            if self.bg_player.playbackState() != QtMultimedia.QMediaPlayer.PlayingState:
                self.bg_player.play()

    def _ensure_bg_playing_state(self, state):
        if state == QtMultimedia.QMediaPlayer.StoppedState:
            self.bg_player.play()

    # --- FG end/stop handlers ---
    def _on_fg_media_status(self, status):
        if status == QtMultimedia.QMediaPlayer.EndOfMedia and self.fg_item.opacity() > 0.0:
            self.fade_back_to_generic()

    def _on_fg_state_changed(self, state):
        if state == QtMultimedia.QMediaPlayer.StoppedState and self.fg_item.opacity() > 0.0:
            self.fade_back_to_generic()

    # --- Anim helper ---
    def _reset_fade_group(self):
        if self.fade_group and self.fade_group.state() == QtCore.QAbstractAnimation.Running:
            self.fade_group.stop()
        self.fade_group = QtCore.QParallelAnimationGroup(self)

    # --- Actions ---
    def crossfade_to_foreground(self, clip_path: str):
        """Show fg on top: fade fg opacity in; audio: bg->down, fg->up."""
        if not os.path.exists(clip_path):
            print(f"[WARN] Missing file: {clip_path}")
            return

        self.fg_player.setSource(url(clip_path))
        self.fg_player.setPosition(0)
        self.fg_player.play()

        self._reset_fade_group()

        # Visual: only fg opacity animates
        fg_op_in = QtCore.QPropertyAnimation(self.fg_item, b"opacity", self)
        fg_op_in.setDuration(FADE_MS)
        fg_op_in.setStartValue(self.fg_item.opacity())
        fg_op_in.setEndValue(1.0)

        # Audio crossfade
        def vol01(p): return max(0.0, min(1.0, p / 100.0))
        fg_target = vol01(TARGET_VOLUME)

        fg_vol_up = QtCore.QVariantAnimation(self)
        fg_vol_up.setDuration(FADE_MS)
        fg_vol_up.setStartValue(self.fg_audio.volume())
        fg_vol_up.setEndValue(fg_target)
        fg_vol_up.valueChanged.connect(lambda v: self.fg_audio.setVolume(float(v)))

        bg_vol_down = QtCore.QVariantAnimation(self)
        bg_vol_down.setDuration(FADE_MS)
        bg_vol_down.setStartValue(self.bg_audio.volume())
        bg_vol_down.setEndValue(0.0)
        bg_vol_down.valueChanged.connect(lambda v: self.bg_audio.setVolume(float(v)))

        self.fade_group.addAnimation(fg_op_in)
        self.fade_group.addAnimation(fg_vol_up)
        self.fade_group.addAnimation(bg_vol_down)
        self.fade_group.start()

    def fade_back_to_generic(self):
        """Hide fg: fade fg opacity out; audio: fg->down, bg->up."""
        self._reset_fade_group()

        fg_op_out = QtCore.QPropertyAnimation(self.fg_item, b"opacity", self)
        fg_op_out.setDuration(FADE_MS)
        fg_op_out.setStartValue(self.fg_item.opacity())
        fg_op_out.setEndValue(0.0)

        fg_vol_down = QtCore.QVariantAnimation(self)
        fg_vol_down.setDuration(FADE_MS)
        fg_vol_down.setStartValue(self.fg_audio.volume())
        fg_vol_down.setEndValue(0.0)
        fg_vol_down.valueChanged.connect(lambda v: self.fg_audio.setVolume(float(v)))

        bg_vol_up = QtCore.QVariantAnimation(self)
        bg_vol_up.setDuration(FADE_MS)
        bg_vol_up.setStartValue(self.bg_audio.volume())
        bg_vol_up.setEndValue(max(0.0, min(1.0, TARGET_VOLUME / 100.0)))
        bg_vol_up.valueChanged.connect(lambda v: self.bg_audio.setVolume(float(v)))

        self.fade_group.addAnimation(fg_op_out)
        self.fade_group.addAnimation(fg_vol_down)
        self.fade_group.addAnimation(bg_vol_up)

        def on_done():
            self.fg_player.stop()
        self.fade_group.finished.connect(on_done)
        self.fade_group.start()

class App(QtWidgets.QApplication):
    def __init__(self, argv):
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(True)

        # Main UI
        self.window = VideoFader()
        screens = QtWidgets.QApplication.screens()
        if len(screens) > 1:
            self.window.setScreen(screens[1])
        self.window.showFullScreen()

        if TEST_MODE:
            print("Keyboard test mode active. Keys:")
            print("  1 → PLAY1   |   2 → PLAY2   |   3 → PLAY3   |   S → STOP   |   Esc → Quit")
            self.installEventFilter(self)
        else:
            self.serial = SerialPoller(PORT, BAUD)
            self.serial.line.connect(self.on_serial_line)
            self.serial.error.connect(lambda m: print("[Serial]", m))
            self.serial.start()

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.KeyPress:
            key = event.key()
            if key == QtCore.Qt.Key_Escape:
                self.quit()
            elif key == QtCore.Qt.Key_1:
                self.on_serial_line("PLAY1")
            elif key == QtCore.Qt.Key_2:
                self.on_serial_line("PLAY2")
            elif key == QtCore.Qt.Key_3:
                self.on_serial_line("PLAY3")
            elif key == QtCore.Qt.Key_4:
                self.on_serial_line("PLAY4")
            elif key == QtCore.Qt.Key_S:
                self.on_serial_line("STOP")
        return super().eventFilter(obj, event)

    @QtCore.Slot(str)
    def on_serial_line(self, msg: str):
        print("[CMD]", msg)
        if msg == "STOP":
            self.window.fade_back_to_generic()
        elif msg in VIDEO_MAP:
            self.window.crossfade_to_foreground(VIDEO_MAP[msg])

if __name__ == "__main__":
    app = App(sys.argv)
    sys.exit(app.exec())
