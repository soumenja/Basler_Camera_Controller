#!/usr/bin/env python3
"""
Basler Live Acquisition App (Python)
=====================================
Simplified, crash-hardened rewrite of `basler_fast_app_V9_singlefile.m`.

Requirements
------------
    pip install pypylon opencv-python numpy PyQt5 tifffile

Also requires the Basler pylon SDK to be installed on the machine
(pypylon links against it) - https://www.baslerweb.com/en/software/pylon/

Features
--------
1. Live view, decoupled camera-grab thread + fast GUI redraw timer
   (LatestImageOnly grab strategy -> no lag build-up, high refresh rate).
2. Exposure time control (ms).
3. Video recording - click Record, type how long to record (seconds).
4. Save path:  <SaveRoot>\\<User>\\<Cohort>\\<MouseID>\\<MouseName_MouseID_Day>.tif
   (multi-page TIFF stack - opens directly in Fiji/ImageJ)
5. Fiji-style Brightness & Contrast panel (Min / Max / Brightness / Contrast
   sliders + Auto / Reset / Set / Apply), applied only to the *display*
   (raw data recorded to disk is untouched).

Crash-hardening
----------------
- Frames are PULLED by the GUI on a fixed timer (default 30 Hz), not
  pushed per-grab via a Qt signal. Basler cameras commonly grab far
  faster than a GUI can render; pushing every frame through a
  cross-thread signal floods Qt's event queue and can take the whole
  app down. Camera speed and GUI speed are now fully decoupled.
- Every hardware call (camera, file IO) is wrapped in try/except.
- The camera-grab loop never busy-spins on failure; it backs off and
  keeps retrying / reconnecting instead of raising into the GUI thread.
- Connection goes through explicit, individually-logged steps
  (enumerate -> create -> open -> configure -> start grab) so a stall
  or failure shows exactly where it happened in ~/basler_app_error.log.
- faulthandler is enabled at startup, so even a true native crash
  (e.g. inside the pylon driver, which Python can't catch) dumps a
  stack trace to the log instead of vanishing silently.
- A global Qt exception hook logs unexpected errors to a file and shows
  a dialog instead of silently killing the app.
- QImage buffers are copied before handing to Qt, avoiding dangling
  pointer crashes from numpy arrays being reused/overwritten.
- Camera buffer pool is capped (MaxNumBuffer=4) to avoid runaway
  memory if the GUI can't keep up with the camera.
- closeEvent() always stops the worker thread and releases the camera,
  even if a recording is mid-flight (with a confirmation prompt).
"""

import sys
import os
import re
import time
import threading
import traceback
import faulthandler
import ctypes
from datetime import datetime

import numpy as np
import cv2
import tifffile

from PyQt5 import QtCore, QtGui, QtWidgets

try:
    from pypylon import pylon
    PYLON_AVAILABLE = True
except Exception:
    PYLON_AVAILABLE = False

APP_TITLE = "Basler Live Acquisition"
CANDIDATE_ROOTS = [r"D:\Widefield Microscopy Data", r"C:\Data"]
ERROR_LOG = os.path.join(os.path.expanduser("~"), "basler_app_error.log")

# On Windows, several camera vendor SDKs (including Basler's pylon
# transport layer) touch COM internally. COM requires each thread that
# uses it to explicitly initialize a threading model first - Qt's
# QThread does NOT do this for you. Calling into the driver from a
# thread that never initialized COM is a well-known cause of a silent,
# instant, unrecoverable process crash (no Python exception, no
# traceback, nothing in the console) - which matches "pops up and
# vanishes" exactly. We initialize/uninitialize COM around the worker
# thread's lifetime as a precaution; this is a no-op and harmless on
# non-Windows platforms.
_IS_WINDOWS = sys.platform.startswith("win")


def _com_initialize():
    if not _IS_WINDOWS:
        return
    try:
        # COINIT_MULTITHREADED = 0x0
        ctypes.windll.ole32.CoInitializeEx(None, 0x0)
    except Exception:
        pass


def _com_uninitialize():
    if not _IS_WINDOWS:
        return
    try:
        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass

# Catches true native crashes (e.g. a segfault inside the pylon driver)
# that a Python try/except cannot catch, and dumps the Python-level
# stack at the moment of the fault into the log file instead of the
# app silently vanishing.
try:
    _fault_fp = open(ERROR_LOG, "a")
    faulthandler.enable(file=_fault_fp, all_threads=True)
except Exception:
    pass


def log_line(msg):
    """Lightweight, immediately-flushed line to the log file, so if the
    process dies hard the last logged step tells us where it got stuck."""
    try:
        with open(ERROR_LOG, "a") as f:
            f.write(f"[{datetime.now()}] {msg}\n")
            f.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def get_save_root():
    """Pick D:\\Widefield Microscopy Data if that drive exists, else C:\\Data, else a home-dir fallback."""
    for path in CANDIDATE_ROOTS:
        drive = os.path.splitdrive(path)[0] + os.sep
        if os.path.exists(drive):
            try:
                os.makedirs(path, exist_ok=True)
                return path
            except Exception:
                continue
    fallback = os.path.join(os.path.expanduser("~"), "BaslerData")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def sanitize(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name if name else "unspecified"


def log_exception(prefix, exc):
    try:
        with open(ERROR_LOG, "a") as f:
            f.write(f"\n[{datetime.now()}] {prefix}\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


# ---------------------------------------------------------------------
# Camera worker thread - all hardware access happens here, never in the
# GUI thread. Communicates with the GUI only through Qt signals.
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# Camera worker - all hardware access happens here, never in the GUI
# thread. Communicates with the GUI only through Qt signals.
#
# NOTE: this is a QObject running on a plain Python threading.Thread,
# NOT a QThread. It was originally a QThread; when the app kept dying
# silently at connect time on one particular machine even after adding
# COM initialization, while an identical plain-Python script connected
# to the same camera without issue every time, that pointed at
# something specific to Qt's own QThread machinery interacting badly
# with the pylon driver on that system. A plain OS thread sidesteps
# whatever that is, while keeping identical signal-based communication
# with the GUI (Qt queues signals based on the *receiving* QObject's
# thread affinity, not the emitting thread's type, so this works the
# same way as before from the GUI's point of view).
# ---------------------------------------------------------------------
class CameraWorker(QtCore.QObject):
    # NOTE: no per-frame signal. Emitting a fresh numpy array through a
    # cross-thread Qt signal on every single grab is the classic way to
    # take an app down once the camera runs faster than the GUI can
    # drain the event queue (Basler cameras easily hit 100-300+ fps).
    # Instead the worker just parks the latest frame behind a lock and
    # the GUI *pulls* it on its own timer via get_latest_frame(). This
    # fully decouples camera speed from GUI speed - the camera can run
    # as fast as it wants without ever flooding Qt.
    status_changed = QtCore.pyqtSignal(str, str)          # message, level
    camera_connected = QtCore.pyqtSignal(bool)
    recording_progress = QtCore.pyqtSignal(int, float)    # frames, seconds remaining
    recording_finished = QtCore.pyqtSignal(str, int)      # filepath, frame count
    recording_failed = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._running = True
        self._camera = None
        self._lock = threading.Lock()
        self._thread = None

        self._frame_lock = threading.Lock()
        self._latest_frame = None
        self._frame_count = 0          # incremented on every grab, for fps calc
        self._frame_seq = 0            # monotonic, never reset - lets the GUI
                                        # detect "is this actually a new grab"

        self._record_flag = threading.Event()
        self._record_path = None
        self._record_duration = 0.0
        self._rec_writer = None
        self._rec_start_time = None
        self._rec_frame_count = 0
        self._rec_last_progress_emit = 0.0

        self._exposure_request_us = None
        self._exposure_desired_us = None   # last value the user asked for; re-applied on every (re)connect
        self._reconnect_requested = threading.Event()
        self._grab_timeout_ms = 200    # scales with exposure - see _apply_pending_exposure
        self._reconnect_failures = 0   # drives backoff so a flaky USB link
                                        # can't hammer the native driver

    # -- thread lifecycle (drop-in replacements for QThread's) -------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name="CameraWorkerThread", daemon=True
        )
        self._thread.start()

    def wait(self, timeout_ms=None):
        if self._thread is None:
            return True
        timeout_s = None if timeout_ms is None else timeout_ms / 1000.0
        self._thread.join(timeout=timeout_s)
        return not self._thread.is_alive()

    # -- public, thread-safe getters used by the GUI -----------------
    def get_latest_frame(self):
        with self._frame_lock:
            return self._latest_frame

    def get_latest_frame_and_seq(self):
        """Same as get_latest_frame() but also returns a monotonic grab
        sequence number, so a caller can tell whether this is genuinely
        a new frame since the last time it asked (used for live-view
        averaging, so the same still-frame isn't counted twice when the
        camera's real fps is lower than the display's refresh rate)."""
        with self._frame_lock:
            return self._latest_frame, self._frame_seq

    def get_and_reset_frame_count(self):
        with self._frame_lock:
            n = self._frame_count
            self._frame_count = 0
            return n

    # -- main loop -------------------------------------------------
    def _run_loop(self):
        _com_initialize()
        log_line("worker thread started")
        try:
            self._connect_camera()
            while self._running:
                if self._camera is None:
                    # Exponential backoff on repeated connect failures -
                    # a flaky/degraded USB link (e.g. negotiating down
                    # to USB2 on a camera that requires USB3) can make
                    # every attempt fail; hammering pylon's native
                    # EnumerateDevices/CreateDevice every 0.5s in that
                    # state is what previously triggered a hard access
                    # violation inside the driver itself. Capped at 5s.
                    delay = min(0.5 * (1.5 ** self._reconnect_failures), 5.0)
                    time.sleep(delay)
                    if self._running:
                        self._connect_camera()
                    continue

                if self._reconnect_requested.is_set():
                    # Manual reconnect: tear down on THIS thread, so the camera is never
                    # closed while RetrieveResult is using it.
                    self._reconnect_requested.clear()
                    self._drop_camera()
                    continue

                self._apply_pending_exposure()

                try:
                    grab = self._camera.RetrieveResult(
                        self._grab_timeout_ms, pylon.TimeoutHandling_Return
                    )
                except Exception as e:
                    self.status_changed.emit(f"Grab error: {e}", "error")
                    log_exception("grab error", e)
                    time.sleep(0.1)
                    self._drop_camera()
                    continue

                # IMPORTANT: on a timeout, RetrieveResult does NOT return
                # Python None - it returns an "empty" result object.
                # Calling GrabSucceeded() (or almost anything else) on
                # that empty object raises a native RuntimeException
                # ("Cannot access NULL pointer"). IsValid() is the
                # correct, exception-free way to detect this case.
                if grab is None or not grab.IsValid():
                    continue

                try:
                    if grab.GrabSucceeded():
                        img = grab.GetArray()
                        if img is not None and img.size > 0:
                            frame = np.ascontiguousarray(img)
                            with self._frame_lock:
                                self._latest_frame = frame
                                self._frame_count += 1
                                self._frame_seq += 1
                            if self._record_flag.is_set():
                                self._write_recording_frame(frame)
                except Exception as e:
                    self.status_changed.emit(f"Frame processing error: {e}", "error")
                    log_exception("frame processing", e)
                finally:
                    try:
                        grab.Release()
                    except Exception:
                        pass

            self._cleanup()
            log_line("worker thread exiting normally")
        except Exception as e:
            # Belt-and-braces: if something unexpected escapes the loop
            # entirely, log it rather than letting the thread die silently.
            log_exception("worker thread top-level", e)
            log_line(f"worker thread exiting after unhandled error: {e}")
        finally:
            _com_uninitialize()

    def stop(self):
        self._running = False

    # -- camera lifecycle -------------------------------------------
    def _connect_camera(self):
        if not PYLON_AVAILABLE:
            self.status_changed.emit("pypylon is not installed", "error")
            self.camera_connected.emit(False)
            return

        self.status_changed.emit("Looking for camera...", "info")
        log_line("connect: enumerating devices")
        try:
            tl_factory = pylon.TlFactory.GetInstance()
            devices = tl_factory.EnumerateDevices()
        except Exception as e:
            self.status_changed.emit(f"Device enumeration failed: {e}", "error")
            log_exception("enumerate devices", e)
            self._reconnect_failures += 1
            self.camera_connected.emit(False)
            return

        if len(devices) == 0:
            self.status_changed.emit("No camera found", "error")
            log_line("connect: no devices found")
            self._reconnect_failures += 1
            self.camera_connected.emit(False)
            return

        cam = None
        try:
            log_line("connect: creating device")
            cam = pylon.InstantCamera(tl_factory.CreateDevice(devices[0]))

            log_line("connect: opening device")
            self.status_changed.emit("Opening camera...", "info")
            cam.Open()

            # Keep the internal buffer pool small - large queues of
            # undelivered frames are a common source of runaway memory
            # and instability when the GUI can't keep up.
            try:
                cam.MaxNumBuffer.Value = 4
            except Exception:
                pass

            try:
                cam.PixelFormat.SetValue("Mono8")
            except Exception as e:
                log_line(f"connect: PixelFormat set skipped ({e})")

            try:
                cam.ExposureAuto.SetValue("Off")
            except Exception as e:
                log_line(f"connect: ExposureAuto set skipped ({e})")

            log_line("connect: starting grab")
            self.status_changed.emit("Starting grab...", "info")
            cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

            with self._lock:
                self._camera = cam
            self._reconnect_failures = 0
            # A fresh connection starts at the camera's default exposure -
            # re-apply whatever the GUI is showing.
            if self._exposure_desired_us is not None:
                self._exposure_request_us = self._exposure_desired_us
            log_line("connect: camera connected successfully")
            self.camera_connected.emit(True)
            self.status_changed.emit("Camera connected", "ok")

        except Exception as e:
            msg = str(e)
            if "USB 3.0" in msg or "USB 2.0" in msg:
                # A specific, actionable message for the single most
                # common real-world cause of this failure - a genuinely
                # USB3-only camera landing on a port/cable/hub that
                # only negotiates USB2 speeds.
                friendly = (
                    "Camera needs a USB 3.0 connection - it's currently on a "
                    "USB 2.0 port/cable/hub. Plug it directly into a rear "
                    "USB3 port with a certified USB3 cable, no hub."
                )
                self.status_changed.emit(friendly, "error")
            else:
                self.status_changed.emit(f"Camera connect failed: {e}", "error")
            log_exception("camera connect", e)
            log_line(f"connect: FAILED - {e}")
            # best-effort teardown of a partially-opened camera
            if cam is not None:
                try:
                    cam.Close()
                except Exception:
                    pass
            self._camera = None
            self._reconnect_failures += 1
            self.camera_connected.emit(False)

    def _drop_camera(self):
        # A recording cannot survive the camera going away: close the file
        # and tell the GUI, otherwise the Record button stays disabled forever.
        if self._record_flag.is_set():
            self._abort_recording()
            self.recording_failed.emit("Camera disconnected during recording; file is incomplete.")
        with self._lock:
            cam = self._camera
            self._camera = None
        if cam is not None:
            try:
                cam.StopGrabbing()
            except Exception:
                pass
            try:
                cam.Close()
            except Exception:
                pass
        self.camera_connected.emit(False)

    def request_reconnect(self):
        self._reconnect_requested.set()

    # -- exposure -----------------------------------------------------
    def request_exposure_us(self, us):
        self._exposure_desired_us = us
        self._exposure_request_us = us

    def _apply_pending_exposure(self):
        if self._exposure_request_us is None:
            return
        us = self._exposure_request_us
        self._exposure_request_us = None
        try:
            with self._lock:
                cam = self._camera
            if cam is not None:
                cam.ExposureTime.SetValue(float(us))
                # The grab timeout must comfortably exceed the exposure
                # time, or every single grab times out (which is exactly
                # what was happening at 500ms exposure with a fixed
                # 200ms timeout). Generous margin + a floor of 200ms.
                self._grab_timeout_ms = max(200, int(us / 1000.0) + 300)
                self.status_changed.emit(f"Exposure set to {us / 1000:.2f} ms", "ok")
        except Exception as e:
            self.status_changed.emit(f"Could not set exposure: {e}", "error")
            log_exception("set exposure", e)

    # -- recording -----------------------------------------------------
    def is_recording(self):
        return self._record_flag.is_set()

    def start_recording(self, filepath, duration_s):
        if self._record_flag.is_set():
            return
        self._record_path = filepath
        self._record_duration = duration_s
        self._rec_start_time = None
        self._rec_writer = None
        self._rec_frame_count = 0
        self._record_flag.set()

    def _write_recording_frame(self, frame):
        try:
            if self._rec_start_time is None:
                self._rec_start_time = time.time()
                # bigtiff=True removes the classic-TIFF ~4GB cap, which
                # would otherwise only allow ~1700 frames at this
                # camera's resolution (well under a minute of recording).
                # A plain multi-page TIFF stack like this opens directly
                # in Fiji via File > Open or drag-and-drop - no special
                # import step needed.
                self._rec_writer = tifffile.TiffWriter(self._record_path, bigtiff=True)

            self._rec_writer.write(frame, contiguous=True)
            self._rec_frame_count += 1

            elapsed = time.time() - self._rec_start_time
            remaining = max(0.0, self._record_duration - elapsed)

            # Throttle GUI updates during recording too - same flooding
            # risk as the live view if the camera is running fast.
            now = time.time()
            if now - self._rec_last_progress_emit >= 0.2:
                self._rec_last_progress_emit = now
                self.recording_progress.emit(self._rec_frame_count, remaining)

            if elapsed >= self._record_duration:
                self._finish_recording()

        except Exception as e:
            log_exception("recording", e)
            self.recording_failed.emit(str(e))
            self._abort_recording()

    def _finish_recording(self):
        self._record_flag.clear()
        try:
            if self._rec_writer is not None:
                self._rec_writer.close()
        except Exception:
            pass
        path, count = self._record_path, self._rec_frame_count
        self._rec_writer = None
        self.recording_finished.emit(path, count)

    def _abort_recording(self):
        self._record_flag.clear()
        try:
            if self._rec_writer is not None:
                self._rec_writer.close()
        except Exception:
            pass
        self._rec_writer = None

    # -- cleanup ---------------------------------------------------
    def _cleanup(self):
        self._abort_recording()
        with self._lock:
            cam = self._camera
            self._camera = None
        if cam is not None:
            try:
                cam.StopGrabbing()
            except Exception:
                pass
            try:
                cam.Close()
            except Exception:
                pass


# ---------------------------------------------------------------------
# Small dialog for the B&C "Set" button (type exact min/max)
# ---------------------------------------------------------------------
class MinMaxDialog(QtWidgets.QDialog):
    def __init__(self, current_min, current_max, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Display Range")
        form = QtWidgets.QFormLayout(self)

        self.min_spin = QtWidgets.QSpinBox()
        self.min_spin.setRange(0, 254)
        self.min_spin.setValue(current_min)

        self.max_spin = QtWidgets.QSpinBox()
        self.max_spin.setRange(1, 255)
        self.max_spin.setValue(current_max)

        form.addRow("Minimum", self.min_spin)
        form.addRow("Maximum", self.max_spin)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def get_values(self):
        lo = self.min_spin.value()
        hi = self.max_spin.value()
        if hi <= lo:
            hi = lo + 1
        return lo, hi


# ---------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1200, 800)

        self.save_root = get_save_root()

        self.latest_frame = None    # local cache (RAW frame), refreshed each display tick
        self.disp_min = 0
        self.disp_max = 255

        self.camera_fps = 0.0

        self._build_ui()

        self.worker = CameraWorker()
        # NOTE: no frame_ready connection - frames are pulled, not pushed.
        # See CameraWorker's class docstring/comment for why.
        self.worker.status_changed.connect(self.on_status_changed)
        self.worker.camera_connected.connect(self.on_camera_connected)
        self.worker.recording_progress.connect(self.on_recording_progress)
        self.worker.recording_finished.connect(self.on_recording_finished)
        self.worker.recording_failed.connect(self.on_recording_failed)
        self.on_exposure_changed()   # push the GUI's initial exposure to the camera
        self.worker.start()

        # Display redraw timer - pulls the latest frame from the worker
        # at a fixed, GUI-controlled rate. Default 30 Hz; this is the
        # single knob that controls how hard the GUI thread works,
        # completely independent of how fast the camera is grabbing.
        self.display_timer = QtCore.QTimer(self)
        self.display_timer.timeout.connect(self.refresh_display)
        self.display_timer.start(33)  # ~30 Hz

        self.fps_timer = QtCore.QTimer(self)
        self.fps_timer.timeout.connect(self.update_fps_label)
        self.fps_timer.start(1000)

    # ---------------------------------------------------------- UI --
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root_layout = QtWidgets.QHBoxLayout(central)

        # ---- left: video + status ----
        left = QtWidgets.QVBoxLayout()
        root_layout.addLayout(left, stretch=3)

        status_row = QtWidgets.QHBoxLayout()
        self.cam_status_label = QtWidgets.QLabel("● CAMERA: CONNECTING...")
        self.cam_status_label.setStyleSheet("color:orange; font-weight:bold;")
        self.fps_label = QtWidgets.QLabel("Camera: 0.0 fps | Display: -- fps")
        status_row.addWidget(self.cam_status_label)
        status_row.addStretch()
        status_row.addWidget(self.fps_label)
        left.addLayout(status_row)

        self.video_label = QtWidgets.QLabel()
        self.video_label.setMinimumSize(640, 480)
        self.video_label.setAlignment(QtCore.Qt.AlignCenter)
        # Ignored policy: a large pixmap must not stop the window being resized smaller.
        self.video_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        self.video_label.setStyleSheet("background-color:black;")
        left.addWidget(self.video_label, stretch=1)

        self.status_bar_label = QtWidgets.QLabel("READY")
        self.status_bar_label.setStyleSheet("color:lightgreen; font-weight:bold;")
        left.addWidget(self.status_bar_label)

        # ---- right: control panels ----
        right = QtWidgets.QVBoxLayout()
        root_layout.addLayout(right, stretch=1)

        right.addWidget(self._build_session_panel())
        right.addWidget(self._build_acquisition_panel())
        right.addWidget(self._build_bc_panel())
        right.addWidget(self._build_actions_panel())
        right.addStretch()

    def _build_session_panel(self):
        box = QtWidgets.QGroupBox("Session")
        form = QtWidgets.QFormLayout(box)

        self.user_edit = QtWidgets.QLineEdit("Soumen")
        self.cohort_edit = QtWidgets.QLineEdit("Cohort 3")
        self.mousename_edit = QtWidgets.QLineEdit("")
        self.mouseid_edit = QtWidgets.QLineEdit("99")
        self.day_edit = QtWidgets.QLineEdit(datetime.now().strftime("%Y-%m-%d"))

        form.addRow("User", self.user_edit)
        form.addRow("Cohort", self.cohort_edit)
        form.addRow("Mouse Name (optional)", self.mousename_edit)
        form.addRow("Mouse ID", self.mouseid_edit)
        form.addRow("Day", self.day_edit)
        return box

    def _build_acquisition_panel(self):
        box = QtWidgets.QGroupBox("Acquisition")
        form = QtWidgets.QFormLayout(box)

        self.exposure_spin = QtWidgets.QDoubleSpinBox()
        self.exposure_spin.setRange(0.01, 1000.0)
        self.exposure_spin.setDecimals(2)
        self.exposure_spin.setValue(10.0)
        self.exposure_spin.setSuffix(" ms")
        self.exposure_spin.editingFinished.connect(self.on_exposure_changed)
        form.addRow("Exposure", self.exposure_spin)

        self.refresh_spin = QtWidgets.QSpinBox()
        self.refresh_spin.setRange(5, 60)
        self.refresh_spin.setValue(30)
        self.refresh_spin.setSuffix(" Hz")
        self.refresh_spin.valueChanged.connect(self.on_refresh_rate_changed)
        form.addRow("Display refresh", self.refresh_spin)

        return box

    def _build_bc_panel(self):
        box = QtWidgets.QGroupBox("Brightness / Contrast (display only)")
        layout = QtWidgets.QVBoxLayout(box)

        def make_slider_row(label, minimum, maximum, value):
            row = QtWidgets.QHBoxLayout()
            lab = QtWidgets.QLabel(label)
            lab.setFixedWidth(70)
            slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            slider.setRange(minimum, maximum)
            slider.setValue(value)
            val_label = QtWidgets.QLabel(str(value))
            val_label.setFixedWidth(40)
            row.addWidget(lab)
            row.addWidget(slider)
            row.addWidget(val_label)
            layout.addLayout(row)
            return slider, val_label

        self.min_slider, self.min_label = make_slider_row("Minimum", 0, 254, 0)
        self.max_slider, self.max_label = make_slider_row("Maximum", 1, 255, 255)
        self.brightness_slider, self.brightness_label = make_slider_row(
            "Brightness", -127, 127, 0
        )
        self.contrast_slider, self.contrast_label = make_slider_row(
            "Contrast", 20, 2000, 100
        )

        self.min_slider.valueChanged.connect(self.on_min_max_changed)
        self.max_slider.valueChanged.connect(self.on_min_max_changed)
        self.brightness_slider.valueChanged.connect(self.on_brightness_contrast_changed)
        self.contrast_slider.valueChanged.connect(self.on_brightness_contrast_changed)

        btn_row1 = QtWidgets.QHBoxLayout()
        auto_btn = QtWidgets.QPushButton("Auto")
        reset_btn = QtWidgets.QPushButton("Reset")
        auto_btn.clicked.connect(self.on_auto_clicked)
        reset_btn.clicked.connect(self.on_reset_clicked)
        btn_row1.addWidget(auto_btn)
        btn_row1.addWidget(reset_btn)
        layout.addLayout(btn_row1)

        btn_row2 = QtWidgets.QHBoxLayout()
        set_btn = QtWidgets.QPushButton("Set")
        apply_btn = QtWidgets.QPushButton("Apply")
        set_btn.clicked.connect(self.on_set_clicked)
        apply_btn.clicked.connect(self.on_apply_clicked)
        btn_row2.addWidget(set_btn)
        btn_row2.addWidget(apply_btn)
        layout.addLayout(btn_row2)

        return box

    def _build_actions_panel(self):
        box = QtWidgets.QGroupBox("Actions")
        layout = QtWidgets.QVBoxLayout(box)

        self.record_btn = QtWidgets.QPushButton("● Record Video")
        self.record_btn.setStyleSheet("font-weight:bold; padding:8px;")
        self.record_btn.clicked.connect(self.on_record_clicked)
        layout.addWidget(self.record_btn)

        self.snapshot_btn = QtWidgets.QPushButton("Take Snapshot")
        self.snapshot_btn.clicked.connect(self.on_snapshot_clicked)
        layout.addWidget(self.snapshot_btn)

        self.reconnect_btn = QtWidgets.QPushButton("Reconnect Camera")
        self.reconnect_btn.clicked.connect(self.on_reconnect_clicked)
        layout.addWidget(self.reconnect_btn)

        return box

    # ---------------------------------------------------- B&C logic --
    def set_min_max(self, lo, hi):
        lo = int(max(0, min(254, lo)))
        hi = int(max(lo + 1, min(255, hi)))
        self.disp_min, self.disp_max = lo, hi

        for w, val in ((self.min_slider, lo), (self.max_slider, hi)):
            w.blockSignals(True)
            w.setValue(val)
            w.blockSignals(False)
        self.min_label.setText(str(lo))
        self.max_label.setText(str(hi))
        self._sync_brightness_contrast_labels()

    def _sync_brightness_contrast_labels(self):
        center = (self.disp_min + self.disp_max) / 2.0
        width = max(1, self.disp_max - self.disp_min)
        brightness = int(round(center - 127))
        brightness = max(-127, min(127, brightness))
        contrast_pct = int(round((255.0 / width) * 100.0))
        contrast_pct = max(20, min(2000, contrast_pct))

        for w, val in (
            (self.brightness_slider, brightness),
            (self.contrast_slider, contrast_pct),
        ):
            w.blockSignals(True)
            w.setValue(val)
            w.blockSignals(False)
        self.brightness_label.setText(str(brightness))
        self.contrast_label.setText(str(contrast_pct))

    def on_min_max_changed(self):
        lo = self.min_slider.value()
        hi = self.max_slider.value()
        if hi <= lo:
            hi = lo + 1
        self.disp_min, self.disp_max = lo, hi
        self.min_label.setText(str(lo))
        self.max_label.setText(str(hi))
        self._sync_brightness_contrast_labels()

    def on_brightness_contrast_changed(self):
        brightness = self.brightness_slider.value()
        contrast_pct = max(1, self.contrast_slider.value())
        center = 127 + brightness
        width = 255.0 * 100.0 / contrast_pct
        lo = center - width / 2.0
        hi = center + width / 2.0
        lo = max(0, lo)
        hi = min(255, hi)
        if hi <= lo:
            hi = lo + 1
        self.disp_min = int(round(lo))
        self.disp_max = int(round(hi))

        for w, val in ((self.min_slider, self.disp_min), (self.max_slider, self.disp_max)):
            w.blockSignals(True)
            w.setValue(val)
            w.blockSignals(False)
        self.min_label.setText(str(self.disp_min))
        self.max_label.setText(str(self.disp_max))
        self.brightness_label.setText(str(brightness))
        self.contrast_label.setText(str(contrast_pct))

    def on_auto_clicked(self):
        if self.latest_frame is None:
            return
        try:
            lo = int(np.percentile(self.latest_frame, 0.35))
            hi = int(np.percentile(self.latest_frame, 99.65))
            if hi <= lo:
                lo, hi = 0, 255
            self.set_min_max(lo, hi)
            self.set_status("Auto B&C applied", "ok")
        except Exception as e:
            self.set_status(f"Auto B&C failed: {e}", "error")

    def on_reset_clicked(self):
        self.set_min_max(0, 255)
        self.set_status("B&C reset to full range", "ok")

    def on_set_clicked(self):
        dlg = MinMaxDialog(self.disp_min, self.disp_max, self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            lo, hi = dlg.get_values()
            self.set_min_max(lo, hi)

    def on_apply_clicked(self):
        # Display LUT is already live; Apply just confirms/redraws.
        self.refresh_display()
        self.set_status(
            f"B&C applied: min={self.disp_min}, max={self.disp_max}", "ok"
        )

    # --------------------------------------------------- exposure --
    def on_exposure_changed(self):
        ms = self.exposure_spin.value()
        self.worker.request_exposure_us(ms * 1000.0)

    def on_refresh_rate_changed(self, hz):
        interval_ms = max(5, int(1000.0 / max(1, hz)))
        self.display_timer.setInterval(interval_ms)

    # --------------------------------------------------- recording --
    def build_save_path(self):
        user = sanitize(self.user_edit.text())
        cohort = sanitize(self.cohort_edit.text())
        mouse_id = sanitize(self.mouseid_edit.text())
        mouse_name = self.mousename_edit.text().strip()
        day = sanitize(self.day_edit.text() or datetime.now().strftime("%Y-%m-%d"))

        folder = os.path.join(self.save_root, user, cohort, mouse_id)
        os.makedirs(folder, exist_ok=True)

        base_name = f"{sanitize(mouse_name)}_{mouse_id}_{day}" if mouse_name else f"{mouse_id}_{day}"
        filepath = os.path.join(folder, base_name + ".tif")
        counter = 2
        while os.path.exists(filepath):
            filepath = os.path.join(folder, f"{base_name}_{counter}.tif")
            counter += 1
        return filepath

    def on_record_clicked(self):
        if self.worker.is_recording():
            return
        if not PYLON_AVAILABLE or self.latest_frame is None:
            QtWidgets.QMessageBox.warning(self, "No Camera", "No live frame available to record.")
            return

        duration, ok = QtWidgets.QInputDialog.getDouble(
            self, "Record Duration", "Seconds to record:", 30.0, 0.1, 36000.0, 1
        )
        if not ok:
            return

        try:
            filepath = self.build_save_path()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Save Path Error", str(e))
            return

        self.record_btn.setEnabled(False)
        self.record_btn.setText("● RECORDING...")
        self.set_status(f"Recording to {filepath}", "ok")
        self.worker.start_recording(filepath, duration)

    def on_recording_progress(self, frames, remaining):
        self.set_status(f"RECORDING | {frames} frames | {remaining:0.1f}s left", "ok")

    def on_recording_finished(self, filepath, frames):
        self.record_btn.setEnabled(True)
        self.record_btn.setText("● Record Video")
        self.set_status(f"SAVED: {filepath} ({frames} frames)", "ok")

    def on_recording_failed(self, message):
        self.record_btn.setEnabled(True)
        self.record_btn.setText("● Record Video")
        self.set_status(f"RECORDING FAILED: {message}", "error")
        QtWidgets.QMessageBox.critical(self, "Recording Failed", message)

    # --------------------------------------------------- snapshot --
    def on_snapshot_clicked(self):
        if self.latest_frame is None:
            QtWidgets.QMessageBox.warning(self, "No Frame", "No live frame available.")
            return
        try:
            user = sanitize(self.user_edit.text())
            cohort = sanitize(self.cohort_edit.text())
            mouse_id = sanitize(self.mouseid_edit.text())
            mouse_name = self.mousename_edit.text().strip()
            day = sanitize(self.day_edit.text() or datetime.now().strftime("%Y-%m-%d"))
            ts = datetime.now().strftime("%H%M%S")

            folder = os.path.join(self.save_root, user, cohort, mouse_id)
            os.makedirs(folder, exist_ok=True)

            parts = [user, cohort]
            if mouse_name:
                parts.append(sanitize(mouse_name))
            parts += [mouse_id, day, ts]
            base_name = "_".join(parts)

            path = os.path.join(folder, base_name + ".png")
            counter = 2
            while os.path.exists(path):
                path = os.path.join(folder, f"{base_name}_{counter}.png")
                counter += 1

            # imencode + write handles non-ASCII paths, which cv2.imwrite does not on Windows
            ok, buf = cv2.imencode(".png", self.latest_frame)
            if not ok:
                raise RuntimeError("PNG encoding failed")
            with open(path, "wb") as fh:
                fh.write(buf.tobytes())
            self.set_status(f"SNAPSHOT SAVED: {path}", "ok")
        except Exception as e:
            self.set_status(f"Snapshot failed: {e}", "error")
            log_exception("snapshot", e)

    # --------------------------------------------------- misc slots --
    def on_reconnect_clicked(self):
        self.set_status("Reconnecting camera...", "info")
        self.worker.request_reconnect()

    def on_status_changed(self, message, level):
        self.set_status(message, level)

    def on_camera_connected(self, connected):
        if connected:
            self.cam_status_label.setText("● CAMERA: CONNECTED")
            self.cam_status_label.setStyleSheet("color:lightgreen; font-weight:bold;")
        else:
            self.cam_status_label.setText("● CAMERA: NOT FOUND")
            self.cam_status_label.setStyleSheet("color:red; font-weight:bold;")

    def set_status(self, message, level="info"):
        color = {"ok": "lightgreen", "error": "red", "info": "khaki"}.get(level, "khaki")
        self.status_bar_label.setStyleSheet(f"color:{color}; font-weight:bold;")
        self.status_bar_label.setText(message)

    # --------------------------------------------------- display --
    def refresh_display(self):
        # Pull the latest frame from the worker (thread-safe). This is
        # the only place per-frame data crosses from the camera thread
        # to the GUI thread, and it happens at a fixed GUI-controlled
        # rate rather than however fast the camera happens to grab.
        if not PYLON_AVAILABLE:
            return
        frame = self.worker.get_latest_frame()
        if frame is None:
            return
        self.latest_frame = frame  # cached for Auto/Snapshot/Record buttons

        try:
            lo, hi = self.disp_min, self.disp_max
            if hi <= lo:
                hi = lo + 1
            f = frame.astype(np.float32)
            f = (f - lo) * (255.0 / (hi - lo))
            np.clip(f, 0, 255, out=f)
            img8 = np.ascontiguousarray(f.astype(np.uint8))

            h, w = img8.shape[:2]
            qimg = QtGui.QImage(img8.data, w, h, w, QtGui.QImage.Format_Grayscale8).copy()
            pix = QtGui.QPixmap.fromImage(qimg)
            self.video_label.setPixmap(
                pix.scaled(
                    self.video_label.size(),
                    QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation,
                )
            )
        except Exception as e:
            log_exception("display refresh", e)

    def update_fps_label(self):
        # Camera-side frame count is tracked inside the worker thread
        # under its own lock; we just read-and-reset it once a second.
        n = self.worker.get_and_reset_frame_count()
        self.camera_fps = float(n)  # timer fires every 1000 ms, so n == fps
        display_hz = 1000.0 / self.display_timer.interval()
        self.fps_label.setText(
            f"Camera: {self.camera_fps:0.1f} fps | Display: {display_hz:0.0f} fps"
        )

    # --------------------------------------------------- shutdown --
    def closeEvent(self, event):
        if self.worker.is_recording():
            reply = QtWidgets.QMessageBox.question(
                self,
                "Recording in progress",
                "A recording is currently running. Close anyway?\n"
                "The partial file may be incomplete.",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply == QtWidgets.QMessageBox.No:
                event.ignore()
                return

        self.display_timer.stop()
        self.fps_timer.stop()
        self.worker.stop()
        self.worker.wait(3000)
        event.accept()


# ---------------------------------------------------------------------
# Global exception hook - keeps unexpected errors from silently
# crashing the whole application.
# ---------------------------------------------------------------------
def excepthook(exc_type, exc_value, exc_tb):
    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(tb_text, file=sys.stderr)
    try:
        with open(ERROR_LOG, "a") as f:
            f.write(f"\n[{datetime.now()}] UNCAUGHT EXCEPTION\n{tb_text}\n")
    except Exception:
        pass
    try:
        QtWidgets.QMessageBox.critical(
            None,
            "Unexpected Error",
            f"An unexpected error occurred and was logged to:\n{ERROR_LOG}\n\n"
            f"{exc_value}\n\nThe app will keep running.",
        )
    except Exception:
        pass


def main():
    sys.excepthook = excepthook
    app = QtWidgets.QApplication(sys.argv)

    if not PYLON_AVAILABLE:
        QtWidgets.QMessageBox.warning(
            None,
            "pypylon not found",
            "The 'pypylon' package (and the Basler pylon SDK) could not be found.\n"
            "The app will still open, but no camera will be available until you:\n"
            "  pip install pypylon\n"
            "and install the pylon SDK from Basler's website.",
        )

    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
