================================================================
 BASLER LIVE ACQUISITION APP - README
================================================================

QUICK START (new computer, fastest path):
  uv python install 3.12 && uv venv --python 3.12 && .venv\Scripts\activate && uv pip install pypylon opencv-python numpy PyQt5 tifffile && python basler_live_app.py
  (If you don't use uv: install Python 3.12, then
   pip install pypylon opencv-python numpy PyQt5 tifffile,
   then python basler_live_app.py - but see section 4 below,
   you will very likely also need the DLL fix in section 5.3c
   before it stays running.)

Two versions of the app are provided:
  - basler_live_app.py          (includes the Live View averaging feature)
  - basler_live_app_no_avg.py   (identical, without averaging)

Both are otherwise functionally identical and share the same
setup/troubleshooting steps below.


----------------------------------------------------------------
1. REQUIREMENTS
----------------------------------------------------------------

A) Basler pylon SDK (required, installs the camera driver)
   Download and install from:
   https://www.baslerweb.com/en/software/pylon/

B) Python 3.9+ (Windows)

C) Python packages:
   pip install pypylon opencv-python numpy PyQt5 tifffile

   If using uv instead of plain pip (recommended - keeps this
   app's packages isolated, and lets you pin an older Python
   version separately from whatever else is on the machine):
     uv python install 3.12
     uv venv --python 3.12
     .venv\Scripts\activate
     uv pip install pypylon opencv-python numpy PyQt5 tifffile
   Re-run ".venv\Scripts\activate" every time you open a new
   terminal to work on this app.

   Note on Python version: very new Python releases (e.g. 3.13,
   3.14) sometimes don't have pre-built pypylon wheels yet. If
   pip/uv install fails or tries to compile from source, install
   Python 3.12 instead (as above) and use that.

D) Microsoft Visual C++ Redistributable (x64) - install this too,
   even if step C succeeds. See Troubleshooting #4 below for why.
   Download "vc_redist.x64.exe" from:
   https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist


----------------------------------------------------------------
2. RUNNING THE APP
----------------------------------------------------------------

   python basler_live_app.py

   (or basler_live_app_no_avg.py)

Connect the camera to a USB 3.0 port BEFORE launching the app.
It will auto-detect and connect; if not found, it retries
automatically with a backoff delay.


----------------------------------------------------------------
3. USING THE APP
----------------------------------------------------------------

SESSION PANEL
  User / Cohort / Mouse Name (optional) / Mouse ID / Day
  These fields drive both the save folder structure and the
  filenames of everything you record or snapshot. Fill them in
  before recording - the Record/Snapshot buttons don't require
  changing anything else, defaults are ready to go out of the box.

ACQUISITION PANEL
  Exposure (ms)     - applied live to the camera.
  Display refresh   - how fast the on-screen image updates
                       (independent of camera grab speed).

LIVE VIEW PANEL (averaging version only)
  "Show averaged image" - rolling average of the last N frames,
  useful in low light to reduce visible noise. This ONLY affects
  what you see on screen - recordings and snapshots always save
  the real, raw, unaveraged frames.

BRIGHTNESS / CONTRAST PANEL
  Fiji-style Min/Max/Brightness/Contrast sliders + Auto/Reset/Set/
  Apply. Display-only, exactly like the Live View averaging - never
  changes what gets saved to disk.

ACTIONS PANEL
  Record Video    - asks how many seconds to record, then saves a
                     multi-page TIFF stack (opens directly in Fiji/
                     ImageJ via File > Open or drag-and-drop).
  Take Snapshot    - saves the current raw frame as a .png.
  Reconnect Camera - manually forces a fresh connect attempt.


----------------------------------------------------------------
4. SAVE LOCATION AND FILE NAMING
----------------------------------------------------------------

Folder structure:
  <SaveRoot>\<User>\<Cohort>\<MouseID>\

  SaveRoot is auto-picked: D:\Widefield Microscopy Data if that drive exists, else
  C:\Data, else a "BaslerData" folder in your home directory.

Video filename:
  <MouseName_>MouseID_Day.tif
  (auto-numbered _2, _3... if the same name already exists)

Snapshot filename:
  User_Cohort_<MouseName_>MouseID_Day_HHMMSS.png
  (auto-numbered _2, _3... if two are taken in the same second)


----------------------------------------------------------------
5. TROUBLESHOOTING
----------------------------------------------------------------

This section documents every real issue we hit while stabilizing
this app across different machines, in the order you should check
them if something goes wrong on a new computer.

------------------------------------------------
5.1  "No camera found" / camera never connects
------------------------------------------------
- Confirm the camera shows up in Basler's own "pylon Viewer" app
  (installed with the SDK). If it doesn't show there either, this
  is a driver/hardware issue, not something in this script.
- Check Windows Device Manager for driver warning icons on the
  camera or its USB controller.

------------------------------------------------
5.2  Error mentions "USB 2.0 port" / "requires USB 3.0"
------------------------------------------------
Basler USB3 cameras need a genuine USB 3.0 SuperSpeed connection.
This error means the camera negotiated down to USB2 speed.
  - Plug directly into a REAR USB 3.0/3.1 port on the PC.
    Avoid front-panel ports, hubs, and USB extension cables -
    these are the most common cause of silent USB2 fallback.
  - Use the cable that shipped with the camera, or one explicitly
    rated "USB 3.0 SuperSpeed". Cheap/damaged cables silently
    negotiate down to USB2 without any obvious sign.
  - Check Device Manager > Universal Serial Bus controllers - the
    port should show as a USB 3.0/3.1 (xHCI) controller, not just
    "USB Root Hub" (that's USB2).
  - Confirm the link speed directly in Basler's pylon Viewer app.
  - The app itself now uses an exponential backoff (0.5s up to a
    5s cap) when retrying a failed connection, specifically so a
    flaky USB link can't hammer the driver into a worse state -
    see 5.3 below for why that mattered.

------------------------------------------------
5.3  App window opens then instantly disappears
     ("pops up and vanishes", no error text at all)
------------------------------------------------
This is the hardest failure mode: a true native crash (a Windows
"access violation"), which a normal Python try/except CANNOT catch,
so nothing gets printed and nothing gets logged by the app itself.

Diagnosis steps, in order:
  1. Check the log file:  %USERPROFILE%\basler_app_error.log
     The app logs each connection step individually (enumerate ->
     create device -> open -> configure -> start grab), so even a
     hard crash usually leaves a trail of the last successful step.

  2. Check Windows Event Viewer for the real crash cause:
       - Win+X > Event Viewer > Windows Logs > Application
       - Find the red "Application Error" entry at the crash time
       - Look at "Faulting module name" - this tells you which DLL
         actually crashed, which is often NOT what you'd expect.

  3. Run test_camera_only.py (if you still have it) - a minimal,
     no-GUI, no-threading script that just talks to the camera
     directly. If this always works but the full app always
     crashes, the problem is something about the GUI process
     itself, not the camera/driver.

Known root causes we found (roughly in the order we ruled them out):
  a) COM threading - some camera SDKs (including pylon's USB
     transport layer) touch COM internally, which requires each
     thread to call CoInitializeEx before using it. The app already
     does this automatically for its camera thread - no action
     needed unless you're modifying the code.
  b) Qt's QThread specifically interacting badly with the driver on
     some machines - the app uses a plain Python thread instead of
     QThread for exactly this reason - no action needed.
  c) *** THE MOST LIKELY CAUSE ON A NEW MACHINE ***
     A DLL version conflict between PyQt5's own bundled C++ runtime
     and the one pylon expects. Confirmed via Event Viewer showing:
       Faulting module: MSVCP140.dll
       Faulting module path: ...\PyQt5\Qt5\bin\MSVCP140.dll
     PyQt5 ships its own private copy of MSVCP140.dll (Microsoft's
     C++ runtime). Windows loads that private copy in preference to
     the one in System32, and if it's a different/incompatible build
     from what pylon expects, you get an instant, deterministic
     access violation - only when PyQt5 and pylon are both active in
     the same process; a plain camera-only script never hits this.

     THE FIX THAT WORKED:
       1. Install the official Microsoft VC++ Redistributable (x64)
          system-wide (see Requirements, item D). This alone is
          often NOT sufficient by itself (see step 2), but do it
          first regardless.
       2. Navigate DIRECTLY to the exact PyQt5 install that this
          app is actually using - paste the full path into File
          Explorer's address bar rather than clicking through, to
          be certain you're not looking at a different Python
          environment's copy by mistake:
            If using a venv (recommended):
              <project folder>\.venv\Lib\site-packages\PyQt5\Qt5\bin\
            If using a plain/global Python install:
              <your Python install>\Lib\site-packages\PyQt5\Qt5\bin\
       3. Create a subfolder there, e.g. "_bak_dlls"
       4. MOVE (do not delete) these files into that subfolder, if
          present: MSVCP140.dll, VCRUNTIME140.dll, VCRUNTIME140_1.dll

          *** IMPORTANT - exact filename match required ***
          The crash report names the file EXACTLY as "MSVCP140.dll"
          (no suffix). This folder also contains similarly-named
          files like "msvcp140_1.dll" and "msvcp140_2.dll" - these
          are DIFFERENT files and moving only those does NOT fix
          the crash. Use the search box in that folder (type
          "msvcp") to confirm the plain "MSVCP140.dll" is actually
          there and gets moved, not just the "_1"/"_2" variants.
          This exact mix-up happened on one machine and cost extra
          troubleshooting time - double check the filename.

       5. Re-run the app. With PyQt5's private copies out of the way,
          Windows falls back to the System32 versions (installed by
          step 1), which are compatible with pylon.
       6. Note: reinstalling/upgrading PyQt5, or creating a fresh
          venv, restores the bundled DLLs and re-triggers this -
          repeat steps 2-4 whenever that happens.

     If step (c) does not resolve it: the fallback is to port the
     app from PyQt5 to PySide6 (Qt6, a different vendor's packaging
     of Qt that doesn't bundle the same conflicting runtime). This
     is a moderate rewrite (mostly renaming a handful of Qt calls),
     not something to attempt casually - ask for help if you get
     here.

  d) Overlay/hook software (Windows Game Bar, GPU vendor overlays,
     Discord overlay, RTSS/MSI Afterburner, antivirus "application
     hardening") CAN inject into any window-creating process and
     cause crashes like this too. If Event Viewer's faulting module
     points at something other than a pylon or Qt DLL, suspect this
     instead and try disabling overlays one at a time.

------------------------------------------------
5.4  COM PORT conflicts (Arduino / serial devices unrelated to
     the camera itself fighting over the same COM port)
------------------------------------------------
Not a camera issue at all, but can destabilize a machine that also
runs other USB-serial hardware (Arduino boards, GSM/GPS modules,
etc.) alongside the camera.
  1. Unplug all USB-serial devices.
  2. Device Manager > View > "Show hidden devices".
  3. Under "Ports (COM & LPT)" (and "Other devices" / "Universal
     Serial Bus controllers"), right-click and Uninstall any
     greyed-out/faded ghost entries.
  4. If that's not enough: back up the registry (regedit > File >
     Export > All), then navigate to
       HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\COM Name Arbiter
     and delete the "ComDB" value (only that value, not the key).
     Restart - Windows rebuilds it empty, freeing all COM numbers.
  5. Reconnect your devices ONE AT A TIME, letting each finish
     installing before plugging in the next.
  6. Optionally pin each device to a fixed COM port: Device Manager
     > right-click device > Properties > Port Settings > Advanced.

------------------------------------------------
5.5  Red banner: "Frame processing error: ... Cannot access NULL
     pointer ... GrabResultPtr.cpp"
------------------------------------------------
Already fixed in the current version of the app - documenting here
in case it resurfaces after a code change.
  Cause: Basler's RetrieveResult() does NOT return Python None on a
  timeout - it returns an "empty" result object. Calling
  GrabSucceeded() on that empty object throws this exact error.
  The fix: check grab.IsValid() (not "grab is None") before touching
  the result, AND make the grab timeout scale with the exposure
  setting (timeout = exposure + 300ms, minimum 200ms) so a long
  exposure doesn't cause every single grab to time out in the first
  place.

------------------------------------------------
5.6  DeprecationWarning about MaxNumBuffer
------------------------------------------------
Already fixed (uses cam.MaxNumBuffer.Value = 4 rather than the
older direct-assignment syntax). Harmless even if seen - was never
crash-causing, just a console warning.

------------------------------------------------
5.7  General diagnostic checklist for a brand-new machine
------------------------------------------------
  1. pip show PyQt5 PyQt5-Qt5 PyQt5-sip pypylon numpy opencv-python
     - compare against a machine where the app already works, if
       you have one. Version mismatches are a common source of
       subtle crashes.
  2. Confirm the camera works in Basler's pylon Viewer app first,
     completely independent of this script.
  3. Check %USERPROFILE%\basler_app_error.log after any crash.
  4. Check Windows Event Viewer > Windows Logs > Application for
     an "Application Error" entry with a "Faulting module" field.
  5. Work through sections 5.1-5.4 above in order.


----------------------------------------------------------------
6. FILES IN THIS FOLDER
----------------------------------------------------------------
  basler_live_app.py           Main app, with Live View averaging.
  basler_live_app_no_avg.py    Main app, without averaging.
  README.txt                   This file.

  (test_camera_only.py and minimal_qt_camera_test.py, if you still
  have them, are minimal diagnostic scripts used to isolate camera
  driver issues from GUI issues - see section 5.3, step 3. They are
  optional and not required to run the app.)
