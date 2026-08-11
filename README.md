# GentLe-Harvests
A GenICam → ASCOM Alpaca Camera Bridge — Startup Guide  This bridge lets any GenICam/GenTL-compatible industrial camera (IDS, FLIR/Teledyne, Basler, Allied Vision, JAI, Matrix Vision, and others) work with astronomy software that speaks ASCOM Alpaca — N.I.N.A., PHD2, CCDciel, and similar tools.
It does no on-board or SDK-side image processing. Every frame it delivers is the raw sensor data, untouched — no color interpolation, gamma, denoising, or compression. This avoids the kind of slowdown some users report with vendor SDKs that leave image-processing features enabled by default.

This guide assumes no prior Python experience.

1. What you need installed
1a. Python

Install Python 3.10 or newer from python.org. During installation, check the box that says "Add Python to PATH."

1b. Your camera vendor's GenICam/GenTL software

This is the one vendor-specific step. Every GenICam camera brand ships a small software package that installs a GenTL producer — a file ending in .cti — which is how any GenICam-compliant application (this bridge included) talks to the camera.

Vendor	What to install
FLIR / Teledyne	Spinnaker SDK (full SDK, not just the viewer)
IDS	IDS peak
Basler	pylon
Allied Vision	Vimba X

You do not need any Python bindings the vendor offers (e.g. PySpin, ids_peak Python package) — this bridge talks to the camera through the generic .cti file directly, via the open-source harvesters library. Any brand's vendor software works the same way as long as it installs a .cti file somewhere on your system.

After installing, confirm the vendor software can see your camera and take a live image in their own viewer app first (SpinView for FLIR, IDS peak Cockpit for IDS, pylon Viewer for Basler, etc.). This confirms the camera and drivers are working before adding this bridge on top.

1c. Python packages

Open Command Prompt (Windows) or Terminal (Mac/Linux) and run:

pip install harvesters numpy flask
harvesters — the open-source GenICam/GenTL library that talks to the .cti file from step 1b
numpy — used to reshape/handle the raw image data
flask — runs the small local web server that N.I.N.A./PHD2 talk to
2. Where the script goes

Save the bridge script (ids_alpaca_bridge.py or whatever it's named) anywhere convenient — the Desktop is fine. There's nothing to install or register; it's a single Python file you run directly.

No configuration file is needed for a first run. The script auto-detects your GenTL producer and your camera.

3. Running it
Make sure your vendor's camera software (SpinView, Cockpit, pylon Viewer, etc.) is fully closed — only one application can hold the camera open at a time.
Open Command Prompt / Terminal.
Navigate to wherever you saved the script, e.g.:
   cd Desktop
Run it:
   python ids_alpaca_bridge.py

You should see startup output like this:

[*] Found GenTL producer: C:\Program Files\FLIR Systems\Spinnaker\bin64\vs2015\FLIR_GenTL_v140.cti
[*] Available GenICam devices:
    serial_number=XXXXXXXX  model=YOUR-CAMERA-MODEL
[+] Auto-selected serial XXXXXXXX
[*] PixelFormat options: [...]
[*] Binning max: ...
[*] Gain range: ...
[*] Alpaca discovery responder listening on UDP 32227
 * Running on http://127.0.0.1:11111

Leave this window open — it's your camera server. Closing it (or pressing Ctrl+C) disconnects the camera.

If you have more than one GenICam camera connected, the script will tell you and ask you to set TARGET_SERIAL_NUMBER near the top of the file to the specific serial number you want it to use.

4. Connecting from N.I.N.A. or PHD2
In your imaging software's camera selection, choose the ASCOM camera type, then pick whatever "Alpaca" / "AlpacaDynamic" driver option appears — it should discover the bridge automatically on your local network (it broadcasts on UDP port 32227).
Connect. You should see the camera's real name, resolution, gain range, and pixel size appear — all pulled live from the camera itself, not hardcoded.
Try a short test exposure (0.5–1 second) first to confirm images come back correctly before doing anything longer.
5. Things worth knowing
Exposure ceiling varies by camera and by what's currently loaded in the camera's own settings. The bridge reports the camera's real, current maximum exposure — it does not assume or hardcode a number. If exposures longer than a second or two matter to you, check what your specific camera reports for ExposureMax after connecting.
Binning and cropping (AOI) are supported if your camera exposes them, and are automatically rounded to whatever step size your sensor's hardware actually requires (some sensors only accept widths in multiples of 8 pixels, for example) — you don't need to know your sensor's specific alignment rules.
This is a development-mode Flask server, meaning it's meant for local, single-user use on your own imaging PC — not for exposing to the internet or sharing across a network with untrusted users.
6. If something doesn't work
"No GenTL producer found" — the vendor SDK from step 1b isn't installed, or didn't register its .cti file. Reinstall it, making sure to pick the full SDK rather than just a viewer/runtime.
Camera not detected — confirm the vendor's own viewer app (not this bridge) can see and connect to the camera first.
"X cameras found, set TARGET_SERIAL_NUMBER" — open the script in a text editor, find the line TARGET_SERIAL_NUMBER = None near the top, and set it to the serial number shown in the startup output, e.g. TARGET_SERIAL_NUMBER = '12345678'.
N.I.N.A./PHD2 can't find it — make sure no firewall is blocking local UDP port 32227 or TCP port 11111.
