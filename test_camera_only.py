#!/usr/bin/env python3
"""
Minimal pypylon connectivity test - NO Qt, NO threads, NO GUI.

Run this by itself in a terminal:

    python test_camera_only.py

Purpose: isolate whether the crash is coming from pypylon/the camera
driver itself, or from how the main app calls it from a background
thread. Watch the console output:

  - If it prints step-by-step and grabs frames fine here -> the crash
    is specific to the threaded GUI app (most likely a COM/threading
    issue, which the latest version of the app now works around).
  - If THIS script also crashes/vanishes with no error -> the problem
    is at the driver/SDK level (mismatched pylon SDK vs pypylon
    version, corrupted install, permissions, conflicting software
    holding the camera open, etc.) and is worth reinstalling the
    pylon SDK / pypylon over.
"""

import sys
import time

print("Step 1: importing pypylon...", flush=True)
from pypylon import pylon
print("  -> pypylon imported OK", flush=True)

print("Step 2: getting TlFactory instance...", flush=True)
tl_factory = pylon.TlFactory.GetInstance()
print("  -> got TlFactory", flush=True)

print("Step 3: enumerating devices...", flush=True)
devices = tl_factory.EnumerateDevices()
print(f"  -> found {len(devices)} device(s)", flush=True)

if len(devices) == 0:
    print("No camera found. Check the camera is powered, cabled, and", flush=True)
    print("shows up in Basler pylon Viewer.", flush=True)
    sys.exit(1)

for i, d in enumerate(devices):
    print(f"    [{i}] {d.GetFriendlyName()}", flush=True)

print("Step 4: creating device object...", flush=True)
cam = pylon.InstantCamera(tl_factory.CreateDevice(devices[0]))
print("  -> device object created", flush=True)

print("Step 5: opening camera...", flush=True)
cam.Open()
print("  -> camera opened", flush=True)

print("Step 6: setting PixelFormat = Mono8...", flush=True)
try:
    cam.PixelFormat.SetValue("Mono8")
    print("  -> PixelFormat set", flush=True)
except Exception as e:
    print(f"  -> could not set PixelFormat: {e}", flush=True)

print("Step 7: turning ExposureAuto off...", flush=True)
try:
    cam.ExposureAuto.SetValue("Off")
    print("  -> ExposureAuto set to Off", flush=True)
except Exception as e:
    print(f"  -> could not set ExposureAuto: {e}", flush=True)

print("Step 8: starting grab (LatestImageOnly)...", flush=True)
cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
print("  -> grabbing started", flush=True)

print("Step 9: retrieving 20 frames...", flush=True)
got = 0
t0 = time.time()
for i in range(20):
    grab = cam.RetrieveResult(1000, pylon.TimeoutHandling_ThrowException)
    if grab.GrabSucceeded():
        got += 1
        arr = grab.GetArray()
        print(f"    frame {i}: shape={arr.shape}, dtype={arr.dtype}", flush=True)
    grab.Release()
dt = time.time() - t0
print(f"  -> got {got}/20 frames in {dt:.2f}s ({got/dt:.1f} fps)", flush=True)

print("Step 10: stopping and closing...", flush=True)
cam.StopGrabbing()
cam.Close()
print("  -> closed cleanly", flush=True)

print("\nALL STEPS COMPLETED SUCCESSFULLY.", flush=True)
