# Basler Live Acquisition App

Python/PyQt5 live view and recording app for Basler cameras (widefield microscopy). A crash-hardened rewrite of `basler_fast_app_V9_singlefile.m`.

| File | Purpose |
|---|---|
| `basler_live_app.py` | Main app, **with** Live View averaging |
| `basler_live_app_no_avg.py` | Same app, without averaging |
| `test_camera_only.py` | Minimal no-GUI camera test (isolates driver problems from GUI problems) |
| `README.txt` | Original plain-text readme (kept for reference) |

## Quick start

```
uv python install 3.12
uv venv --python 3.12
.venv\Scripts\activate
uv pip install pypylon opencv-python numpy PyQt5 tifffile
python basler_live_app.py
```

Without uv: install Python 3.12, `pip install pypylon opencv-python numpy PyQt5 tifffile`, then run the script. You will likely also need the DLL fix in [Troubleshooting 3c](#3-app-opens-then-instantly-disappears).

## Requirements

- **Basler pylon SDK** (camera driver): https://www.baslerweb.com/en/software/pylon/
- **Python 3.9+** on Windows. Python 3.13/3.14 may lack pypylon wheels; use 3.12.
- **Microsoft VC++ Redistributable (x64)**, installed even if pip succeeds (see Troubleshooting 3c): https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist
- Connect the camera to a **rear USB 3.0 port before launching**. The app auto-detects it and retries with backoff.

## Using the app

- **Session panel:** User / Cohort / Mouse Name (optional) / Mouse ID / Day. These drive the save folders and file names.
- **Acquisition panel:** Exposure (ms), applied live to the camera. Display refresh (Hz) is independent of camera speed.
- **Live View panel** (averaging version): rolling average of the last N frames to reduce noise in low light. Display only; recordings and snapshots are always raw.
- **Brightness / Contrast panel:** Fiji-style Min/Max/Brightness/Contrast sliders plus Auto / Reset / Set / Apply. Display only.
- **Actions:**
  - *Record Video*: asks for a duration in seconds, saves a multi-page BigTIFF (opens in Fiji/ImageJ).
  - *Take Snapshot*: saves the current raw frame as PNG.
  - *Reconnect Camera*: forces a fresh connection.

## Save location and naming

```
<SaveRoot>\<User>\<Cohort>\<MouseID>\
```

`SaveRoot` is chosen automatically:

1. `D:\Widefield Microscopy Data` (if drive D: exists)
2. `C:\Data`
3. `~\BaslerData`

- Video: `<MouseName_>MouseID_Day.tif`
- Snapshot: `User_Cohort_<MouseName_>MouseID_Day_HHMMSS.png`
- Existing names get `_2`, `_3`, ... appended.

## Changelog / add-ons

### Latest (bug fixes and changes)

- **Save root changed** from `D:\Data` to `D:\Widefield Microscopy Data`.
- **Initial exposure now applied.** Previously the GUI showed 100 ms while the camera used its default until the box was edited. The exposure is now sent at startup and re-applied after every reconnect.
- **Reconnect race fixed.** "Reconnect Camera" used to close the camera from the GUI thread while the grab thread was using it (possible native crash). The grab thread now performs the teardown.
- **Camera drop during recording** now aborts and closes the file and shows an error, instead of leaving the Record button disabled forever.
- **Snapshots with non-ASCII paths** now save correctly (PNG encoded with `cv2.imencode` and written with a normal file write, because `cv2.imwrite` fails on such Windows paths).
- **Window resizing:** the video label no longer prevents the window from shrinking after a large frame is shown.

### Earlier features and hardening

- Live view with decoupled camera-grab thread and a fixed-rate GUI timer (frames are pulled, not pushed through Qt signals).
- Grab strategy `LatestImageOnly`, `MaxNumBuffer=4`.
- Plain Python thread (not `QThread`) for the camera, with COM initialised on that thread.
- Exponential reconnect backoff (0.5 s up to 5 s) so a flaky USB link cannot hammer the driver.
- Grab timeout scales with exposure (`exposure + 300 ms`, minimum 200 ms); `IsValid()` is checked before touching a grab result.
- `faulthandler` plus step-by-step logging to `%USERPROFILE%\basler_app_error.log`.
- Global exception hook with a dialog instead of a silent exit.
- Live-view averaging (display only) with a sliding-window accumulator.
- Fiji-style Brightness/Contrast panel (display only).
- BigTIFF recording (no 4 GB limit).

## Troubleshooting

### 1. "No camera found"
Confirm the camera appears in Basler's **pylon Viewer**. If not, it is a driver or hardware issue. Check Device Manager for warning icons.

### 2. "requires USB 3.0" / USB 2.0 error
The camera negotiated down to USB2. Use a **rear USB 3.x port**, no hubs or extension cables, and the supplied or a certified USB3 cable. Verify the link speed in pylon Viewer.

### 3. App opens then instantly disappears
A native access violation that Python cannot catch. Diagnose:

1. Check `%USERPROFILE%\basler_app_error.log` (each connection step is logged).
2. Event Viewer → Windows Logs → Application → find the "Application Error" and read **Faulting module name**.
3. Run `test_camera_only.py`. If it always works while the full app crashes, the problem is in the GUI process.

Known causes:

- **a) COM threading:** already handled (CoInitializeEx on the camera thread).
- **b) QThread vs. driver:** already handled (plain Python thread).
- **c) PyQt5's bundled `MSVCP140.dll` conflicts with pylon (most likely on a new machine).** Event Viewer shows `Faulting module: MSVCP140.dll`, path `...\PyQt5\Qt5\bin\MSVCP140.dll`. Fix:
  1. Install the VC++ Redistributable (x64).
  2. Open `<venv or Python>\Lib\site-packages\PyQt5\Qt5\bin\` (paste the full path into Explorer).
  3. Create a subfolder `_bak_dlls`.
  4. **Move, don't delete**, `MSVCP140.dll`, `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`. The name must match exactly; `msvcp140_1.dll` / `msvcp140_2.dll` are different files and moving only those does not help.
  5. Re-run the app. Repeat after any PyQt5 reinstall or new venv.
  6. Last resort: port to PySide6.
- **d) Overlay/hook software** (Game Bar, GPU overlays, Discord, RTSS/Afterburner, AV hardening). Suspect these if the faulting module is not a pylon or Qt DLL.

### 4. COM port conflicts (Arduino/serial devices)
Not a camera issue, but can destabilise a shared machine.
1. Unplug USB-serial devices.
2. Device Manager → View → Show hidden devices; uninstall greyed-out ghost entries under Ports, Other devices and USB controllers.
3. If needed, back up the registry, then delete only the `ComDB` value under `HKLM\SYSTEM\CurrentControlSet\Control\COM Name Arbiter` and restart.
4. Reconnect devices one at a time; optionally pin COM numbers under Port Settings → Advanced.

### 5. "Cannot access NULL pointer ... GrabResultPtr.cpp"
Fixed in the current version: `RetrieveResult()` returns an empty object on timeout, not `None`, so `grab.IsValid()` is checked, and the timeout scales with exposure.

### 6. DeprecationWarning about MaxNumBuffer
Fixed (`cam.MaxNumBuffer.Value = 4`). Harmless in any case.

### 7. Checklist for a new machine
1. `pip show PyQt5 PyQt5-Qt5 PyQt5-sip pypylon numpy opencv-python` and compare against a working machine.
2. Confirm the camera works in pylon Viewer.
3. Check `%USERPROFILE%\basler_app_error.log`.
4. Check Event Viewer for the faulting module.
5. Work through sections 1 to 4 above.
