import os
import struct
import threading
import time

import numpy as np
from flask import Flask, jsonify, request, Response
from harvesters.core import Harvester

app = Flask(__name__)

print("=" * 60)
print("[*] SCRIPT VERSION MARKER: generic-portable-v3-consolidated")
print("=" * 60)
try:
    mtime = os.path.getmtime(__file__)
    print(f"[*] File last-modified: {time.ctime(mtime)}")
    print(f"[*] Running from: {os.path.abspath(__file__)}")
except Exception as e:
    print(f"[-] Could not read file timestamp: {e}")
print("=" * 60)


def find_cti_path():
    """Searches every GenTL producer registered in the standard
    GENICAM_GENTL64_PATH environment variable -- this variable and the
    .cti file mechanism are a real, open GenICam standard, not IDS-specific.
    Any compliant vendor (Basler, FLIR/Teledyne, Allied Vision, JAI, Matrix
    Vision, IDS, etc.) that installs their own GenTL producer registers it
    here the same way. This prefers a uEye-named producer if multiple are
    installed (since that's the one this script is built and tested
    against), but will use whatever it finds if only one exists."""
    env_path = os.environ.get('GENICAM_GENTL64_PATH', '')
    candidates = []
    for folder in env_path.split(';'):
        if os.path.isdir(folder):
            for filename in os.listdir(folder):
                if filename.lower().endswith('.cti'):
                    candidates.append(os.path.join(folder, filename))

    if not candidates:
        raise SystemExit("[-] No GenTL producer (.cti) found via GENICAM_GENTL64_PATH. "
                          "Make sure your camera vendor's GenICam/GenTL software is installed.")

    ueye_matches = [c for c in candidates if 'ueye' in c.lower()]
    chosen = ueye_matches[0] if ueye_matches else candidates[0]
    if len(candidates) > 1:
        print(f"[*] Multiple GenTL producers found: {candidates}")
        print(f"[+] Using: {chosen}")
    return chosen


CTI_PATH = find_cti_path()
print(f"[*] Found GenTL producer: {CTI_PATH}")

harvester = Harvester()
harvester.add_file(CTI_PATH)
harvester.update()

TARGET_SERIAL_NUMBER = None  # auto-detects if only one camera is connected

print("[*] Available GenICam devices:")
for info in harvester.device_info_list:
    print(f"    serial_number={info.serial_number}  model={info.model}")

ia = None
if TARGET_SERIAL_NUMBER:
    for info in harvester.device_info_list:
        if info.serial_number == TARGET_SERIAL_NUMBER:
            ia = harvester.create(info)
            print(f"[+] Opened camera serial {TARGET_SERIAL_NUMBER}")
            break
    if ia is None:
        raise SystemExit(f"[-] Serial {TARGET_SERIAL_NUMBER} not found.")
else:
    devs = harvester.device_info_list
    if len(devs) == 1:
        ia = harvester.create(0)
        print(f"[+] Auto-selected serial {devs[0].serial_number}")
    else:
        raise SystemExit(f"[-] {len(devs)} cameras found, set TARGET_SERIAL_NUMBER.")

node_map = ia.remote_device.node_map

# Auto-load UserSet0 at startup, BEFORE ia.start(). UserSetLoad is a
# command node that many cameras will not allow while the acquisition
# stream is running -- attempting this after ia.start() silently fails
# with "Node is not writable" on some units, which is why this happens
# here, before the ia.start() call further down.
try:
    node_map.UserSetSelector.value = "UserSet0"
    node_map.UserSetLoad.execute()
    print(f"[+] Loaded UserSet0 -- real ExposureTime.max now: {node_map.ExposureTime.max} us "
          f"({node_map.ExposureTime.max / 1_000_000:.4f} s)")
except Exception as e:
    print(f"[-] Could not load UserSet0 (falling back to Default): {e}")


SENSOR_MAX_WIDTH = int(node_map.WidthMax.value)
SENSOR_MAX_HEIGHT = int(node_map.HeightMax.value)
try:
    PIXEL_SIZE_UM = float(node_map.SensorPixelWidth.value)
except Exception as e:
    print(f"[-] Could not read real pixel size from camera: {e}")
    print("[-] No generic fallback value used -- this is sensor-specific and shouldn't be guessed.")
    PIXEL_SIZE_UM = 0.0

IS_COLOR_SENSOR = False
try:
    formats = list(node_map.PixelFormat.symbolics)
    print(f"[*] PixelFormat options: {formats}")
    pref = ["Mono12", "Mono10", "Mono8", "BayerRG12", "BayerRG10", "BayerRG8"]
    chosen = next((f for f in pref if f in formats), formats[0] if formats else None)
    if chosen:
        node_map.PixelFormat.value = chosen
        print(f"[+] PixelFormat = '{chosen}'")
        IS_COLOR_SENSOR = chosen.startswith("Bayer")
except Exception as e:
    print(f"[-] PixelFormat: {e}")

triggers = list(node_map.TriggerSelector.symbolics)
print(f"[*] TriggerSelector options: {triggers}")
pref_trig = ["FrameStart", "FrameBurstStart", "AcquisitionStart", "ExposureStart"]
chosen_trig = next((t for t in pref_trig if t in triggers), triggers[0] if triggers else None)
if chosen_trig is None:
    raise SystemExit("[-] No TriggerSelector options.")
print(f"[+] TriggerSelector = '{chosen_trig}'")
node_map.TriggerSelector.value = chosen_trig
node_map.TriggerMode.value = "On"
node_map.TriggerSource.value = "Software"
node_map.AcquisitionMode.value = "Continuous"

binning_max_x = 1
binning_max_y = 1
try:
    binning_max_x = int(node_map.BinningHorizontal.max)
    binning_max_y = int(node_map.BinningVertical.max)
    print(f"[*] Binning max: {binning_max_x}x{binning_max_y}")
except Exception as e:
    print(f"[-] Binning: {e}")

HAS_DECIMATION = False
decimation_max_x = 1
decimation_max_y = 1
try:
    decimation_max_x = int(node_map.DecimationHorizontal.max)
    decimation_max_y = int(node_map.DecimationVertical.max)
    print(f"[*] DecimationHorizontal range: {node_map.DecimationHorizontal.min} - {decimation_max_x}")
    print(f"[*] DecimationVertical range: {node_map.DecimationVertical.min} - {decimation_max_y}")
    HAS_DECIMATION = True
except Exception as e:
    print(f"[-] No Decimation (subsampling) node found: {e}")

real_gain_min, real_gain_max = 0, 400
try:
    real_gain_min = int(node_map.Gain.min)
    real_gain_max = int(node_map.Gain.max)
    print(f"[*] Gain range: {real_gain_min}-{real_gain_max}")
except Exception as e:
    print(f"[-] Gain: {e}")

real_offset_min, real_offset_max = 0, 255
HAS_REAL_BLACKLEVEL_NODE = False
try:
    real_offset_min = int(node_map.BlackLevel.min)
    real_offset_max = int(node_map.BlackLevel.max)
    HAS_REAL_BLACKLEVEL_NODE = True
    print(f"[*] BlackLevel range: {real_offset_min}-{real_offset_max}")
except Exception as e:
    print(f"[-] BlackLevel: {e}")

HAS_REAL_TEMPERATURE_NODE = False
temperature_node_name = None
for candidate_name in ['DeviceTemperature', 'SensorTemperature', 'TemperatureAbs']:
    try:
        test_node = getattr(node_map, candidate_name)
        _ = test_node.value
        temperature_node_name = candidate_name
        HAS_REAL_TEMPERATURE_NODE = True
        print(f"[+] Found real temperature node: '{candidate_name}'")
        break
    except Exception:
        continue
if not HAS_REAL_TEMPERATURE_NODE:
    print("[-] No real temperature node found on this camera -- CCDTemperature will stay a placeholder")

ia.start()

state_lock = threading.Lock()
camera_state = 0
image_ready = False
last_image = None
current_exposure_s = 1.0
last_actual_exposure_s = 1.0
current_gain = 0
current_offset = 0
current_decimation_x = 1
current_decimation_y = 1
has_exposed = False  # tracks whether any exposure has ever completed --
                      # LastExposureDuration/LastExposureStartTime must
                      # error if queried before this is True.

aoi_start_x, aoi_start_y = 0, 0
aoi_num_x, aoi_num_y = SENSOR_MAX_WIDTH, SENSOR_MAX_HEIGHT
bin_x, bin_y = 1, 1


def apply_aoi():
    config = (bin_x, bin_y, aoi_start_x, aoi_start_y, aoi_num_x, aoi_num_y,
              current_decimation_x, current_decimation_y)
    if getattr(apply_aoi, 'last_applied', None) == config:
        return
    ia.stop()
    try:
        try:
            node_map.BinningHorizontal.value = bin_x
            node_map.BinningVertical.value = bin_y
        except Exception as e:
            print(f"[-] set binning: {e}")

        if HAS_DECIMATION:
            try:
                node_map.DecimationHorizontal.value = current_decimation_x
                node_map.DecimationVertical.value = current_decimation_y
            except Exception as e:
                print(f"[-] set decimation: {e}")

        node_map.OffsetX.value = 0
        node_map.OffsetY.value = 0

        try:
            wmax = int(node_map.Width.max)
            wmin = int(node_map.Width.min)
            winc = int(getattr(node_map.Width, 'inc', 1)) or 1
        except Exception:
            wmax, wmin, winc = aoi_num_x, aoi_num_x, 1
        try:
            hmax = int(node_map.Height.max)
            hmin = int(node_map.Height.min)
            hinc = int(getattr(node_map.Height, 'inc', 1)) or 1
        except Exception:
            hmax, hmin, hinc = aoi_num_y, aoi_num_y, 1

        # Round down to the nearest legal increment FIRST. Clients that
        # compute ChipWidth / BinX the naive way (e.g. 4104/2=2052) can
        # land one alignment-step above what the sensor's readout hardware
        # actually allows (e.g. max=2048, inc=8) -- that's a normal,
        # expected case and should just work, not error.
        cw = wmin + ((aoi_num_x - wmin) // winc) * winc
        ch = hmin + ((aoi_num_y - hmin) // hinc) * hinc
        cw = max(wmin, min(cw, wmax))
        ch = max(hmin, min(ch, hmax))

        # Only reject requests that are genuinely invalid -- i.e. still
        # out of bounds by more than one increment step even after
        # rounding. This catches truly bogus requests (way beyond max,
        # or below min) while letting near-miss "naive math" requests
        # through cleanly.
        if aoi_num_x > wmax + winc:
            raise ValueError(f"NumX={aoi_num_x} exceeds Width.max={wmax}")
        if aoi_num_y > hmax + hinc:
            raise ValueError(f"NumY={aoi_num_y} exceeds Height.max={hmax}")
        if aoi_num_x < wmin:
            raise ValueError(f"NumX={aoi_num_x} is below Width.min={wmin}")
        if aoi_num_y < hmin:
            raise ValueError(f"NumY={aoi_num_y} is below Height.min={hmin}")

        if cw != aoi_num_x or ch != aoi_num_y:
            print(f"[i] apply_aoi: rounded requested {aoi_num_x}x{aoi_num_y} "
                  f"down to {cw}x{ch} to satisfy Width.inc={winc}/Height.inc={hinc}")

        node_map.Width.value = cw
        node_map.Height.value = ch
        node_map.OffsetX.value = aoi_start_x
        node_map.OffsetY.value = aoi_start_y
    finally:
        ia.start()
    apply_aoi.last_applied = config


def do_exposure(duration_s):
    global camera_state, image_ready, last_image, last_actual_exposure_s, has_exposed
    with state_lock:
        camera_state = 2
        image_ready = False
    try:
        try:
            node_map.AcquisitionFrameRateEnable.value = False
        except Exception:
            try:
                mn, mx = node_map.AcquisitionFrameRate.min, node_map.AcquisitionFrameRate.max
                fps = max(mn, min(1.0 / (duration_s + 0.5), mx))
                node_map.AcquisitionFrameRate.value = fps
            except Exception as e:
                print(f"[-] frame rate: {e}")

        req_us = duration_s * 1_000_000.0
        try:
            emin, emax = node_map.ExposureTime.min, node_map.ExposureTime.max
            clamped_us = max(emin, min(req_us, emax))
        except Exception as e:
            print(f"[-] exposure range: {e}")
            clamped_us = req_us

        node_map.ExposureTime.value = clamped_us
        with state_lock:
            last_actual_exposure_s = clamped_us / 1_000_000.0

        try:
            node_map.Gain.value = current_gain
        except Exception:
            pass
        if HAS_REAL_BLACKLEVEL_NODE:
            try:
                node_map.BlackLevel.value = current_offset
            except Exception as e:
                print(f"[-] black level: {e}")

        # camera_state is already 2 (Exposing) at this point -- stay here
        # for the actual duration of the exposure. Flipping to state 3
        # (Reading) immediately after the trigger fires -- before the
        # real exposure time has elapsed -- misreports the entire
        # exposure window as "Reading" instead of "Exposing", which is
        # why ASCOM clients polling for the Exposing state never catch it.
        node_map.TriggerSoftware.execute()
        actual_exposure_s = clamped_us / 1_000_000.0
        time.sleep(actual_exposure_s)

        with state_lock:
            camera_state = 3

        with ia.fetch(timeout=max(duration_s + 5.0, 10.0)) as buffer:
            component = buffer.payload.components[0]
            frame = component.data.reshape(component.height, component.width).copy()

        with state_lock:
            last_image = frame
            image_ready = True
            camera_state = 0
            has_exposed = True
    except Exception as e:
        print(f"[-] do_exposure: {type(e).__name__}: {e}")
        with state_lock:
            camera_state = 5
            image_ready = False


_server_transaction_id_lock = threading.Lock()
_server_transaction_id_counter = 0


def next_server_transaction_id():
    global _server_transaction_id_counter
    with _server_transaction_id_lock:
        _server_transaction_id_counter += 1
        return _server_transaction_id_counter


def get_param_ci(name, default=None):
    """
    Case-insensitive lookup across both query-string args and form body.
    Per the ASCOM Alpaca spec, ClientID and ClientTransactionID MUST be
    matched case-insensitively -- this is the ONLY place that rule
    applies. Device-specific parameters (Duration, Gain, BinX, etc.) are
    intentionally matched case-SENSITIVELY elsewhere in this file (see
    get_required_int/float/bool below) -- sending them with wrong casing
    is correctly treated as "parameter not supplied".
    """
    name_lower = name.lower()
    for key in request.args:
        if key.lower() == name_lower:
            return request.args[key]
    for key in request.form:
        if key.lower() == name_lower:
            return request.form[key]
    return default


def alpaca_ids():
    try:
        cid = int(get_param_ci("ClientID", 0))
        if cid < 0:
            cid = 0
    except (ValueError, TypeError):
        cid = 0
    try:
        ctid = int(get_param_ci("ClientTransactionID", 0))
        if ctid < 0:
            ctid = 0
    except (ValueError, TypeError):
        ctid = 0
    return cid, ctid


def ok(value=None, server_id=None):
    cid, ctid = alpaca_ids()
    real_server_id = server_id if server_id is not None else next_server_transaction_id()
    body = {"ClientTransactionID": ctid, "ServerTransactionID": real_server_id,
            "ErrorNumber": 0, "ErrorMessage": ""}
    if value is not None:
        body["Value"] = value
    return jsonify(body)


def ascom_error(error_number, message, http_status=200):
    """Proper ASCOM-style error response (used for out-of-range values,
    not-implemented features, camera-busy, etc.) -- these are valid
    requests that the device legitimately rejects, so HTTP 200 with an
    ASCOM ErrorNumber is correct per spec."""
    cid, ctid = alpaca_ids()
    return jsonify({"ErrorNumber": error_number, "ErrorMessage": message,
                     "ClientTransactionID": ctid, "ServerTransactionID": next_server_transaction_id()}), http_status


class MissingOrInvalidParameter(Exception):
    """Raised when a required device-specific parameter is missing
    (including via wrong casing, which counts as missing) or fails to
    parse as the expected type. This should become a true HTTP 400,
    NOT an ASCOM-error-with-200 -- the request itself is malformed."""
    pass


def get_required_int(name):
    # Case-SENSITIVE on purpose -- see get_param_ci docstring above.
    raw = request.form.get(name, request.args.get(name))
    if raw is None:
        raise MissingOrInvalidParameter(f"Missing required parameter: {name}")
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        raise MissingOrInvalidParameter(f"Parameter {name} is not a valid number: {raw!r}")


def get_required_float(name):
    raw = request.form.get(name, request.args.get(name))
    if raw is None:
        raise MissingOrInvalidParameter(f"Missing required parameter: {name}")
    try:
        return float(raw)
    except (ValueError, TypeError):
        raise MissingOrInvalidParameter(f"Parameter {name} is not a valid number: {raw!r}")


def get_required_bool(name):
    raw = request.form.get(name, request.args.get(name))
    if raw is None:
        raise MissingOrInvalidParameter(f"Missing required parameter: {name}")
    if str(raw).lower() not in ("true", "false"):
        raise MissingOrInvalidParameter(f"Parameter {name} is not a valid boolean: {raw!r}")
    return str(raw).lower() == "true"


@app.errorhandler(MissingOrInvalidParameter)
def handle_missing_param(e):
    # A genuinely malformed/missing required parameter is a true HTTP
    # 400 Bad Request -- not an ASCOM ErrorNumber wrapped in a 200. This
    # is what was previously missing and caused Flask's default 500
    # error page to appear instead whenever a value failed to parse.
    return jsonify({"ErrorMessage": str(e)}), 400


@app.route('/management/apiversions', methods=['GET'])
def m_apiversions():
    return ok([1])


@app.route('/management/v1/description', methods=['GET'])
def m_description():
    return ok({"ServerName": "IDS uEye Alpaca Bridge", "Manufacturer": "DIY",
               "ManufacturerVersion": "0.1", "Location": "Local"})


@app.route('/management/v1/configureddevices', methods=['GET'])
def m_configureddevices():
    return ok([{"DeviceName": "IDS Camera", "DeviceType": "Camera",
                "DeviceNumber": 0, "UniqueID": "ids-cam-0"}])


is_connected = True


@app.route('/api/v1/camera/0/connected', methods=['GET', 'PUT'])
def connected():
    global is_connected
    if request.method == 'PUT':
        is_connected = get_required_bool('Connected')
        return ok()
    return ok(is_connected)


@app.route('/api/v1/camera/0/description', methods=['GET'])
def description():
    return ok("IDS Camera via GenICam/harvesters Alpaca bridge")


@app.route('/api/v1/camera/0/name', methods=['GET'])
def name():
    return ok("IDS Camera")


@app.route('/api/v1/camera/0/interfaceversion', methods=['GET'])
def interfaceversion():
    return ok(3)


@app.route('/api/v1/camera/0/driverversion', methods=['GET'])
def driverversion():
    return ok("2.0")


@app.route('/api/v1/camera/0/driverinfo', methods=['GET'])
def driverinfo():
    return ok("IDS GenICam/Alpaca bridge -- generic portable version")


@app.route('/api/v1/camera/0/supportedactions', methods=['GET'])
def supportedactions():
    return ok([])


@app.route('/api/v1/camera/0/sensortype', methods=['GET'])
def sensortype():
    return ok(2 if IS_COLOR_SENSOR else 0)


@app.route('/api/v1/camera/0/sensorname', methods=['GET'])
def sensorname():
    return ok("IDS Camera")


@app.route('/api/v1/camera/0/hasshutter', methods=['GET'])
def hasshutter():
    return ok(False)


@app.route('/api/v1/camera/0/bayeroffsetx', methods=['GET'])
def bayeroffsetx():
    if IS_COLOR_SENSOR:
        return ok(0)
    return ascom_error(1024, "Not implemented -- monochrome sensor")


@app.route('/api/v1/camera/0/bayeroffsety', methods=['GET'])
def bayeroffsety():
    if IS_COLOR_SENSOR:
        return ok(0)
    return ascom_error(1024, "Not implemented -- monochrome sensor")


@app.route('/api/v1/camera/0/canasymmetricbin', methods=['GET'])
def canasymmetricbin():
    return ok(True)


@app.route('/api/v1/camera/0/canabortexposure', methods=['GET'])
def canabortexposure():
    return ok(True)


@app.route('/api/v1/camera/0/canstopexposure', methods=['GET'])
def canstopexposure():
    return ok(True)


@app.route('/api/v1/camera/0/canfastreadout', methods=['GET'])
def canfastreadout():
    return ok(False)


@app.route('/api/v1/camera/0/fastreadout', methods=['GET', 'PUT'])
def fastreadout():
    return ascom_error(1024, "Not implemented -- CanFastReadout is False")


@app.route('/api/v1/camera/0/cangetcoolerpower', methods=['GET'])
def cangetcoolerpower():
    return ok(False)


@app.route('/api/v1/camera/0/cansetccdtemperature', methods=['GET'])
def cansetccdtemperature():
    return ok(False)


@app.route('/api/v1/camera/0/cooleron', methods=['GET', 'PUT'])
def cooleron():
    if request.method == 'PUT':
        get_required_bool('CoolerOn')  # validated for conformance; this camera has no real cooler
        return ok()
    return ok(False)


@app.route('/api/v1/camera/0/ccdtemperature', methods=['GET'])
def ccdtemperature():
    if HAS_REAL_TEMPERATURE_NODE:
        try:
            return ok(float(getattr(node_map, temperature_node_name).value))
        except Exception as e:
            print(f"[-] temperature read: {e}")
    return ok(20.0)


@app.route('/api/v1/camera/0/heatsinktemperature', methods=['GET'])
def heatsinktemperature():
    return ok(20.0)


@app.route('/api/v1/camera/0/coolerpower', methods=['GET'])
def coolerpower():
    return ok(0.0)


@app.route('/api/v1/camera/0/electronsperadu', methods=['GET'])
def electronsperadu():
    return ok(1.0)


@app.route('/api/v1/camera/0/fullwellcapacity', methods=['GET'])
def fullwellcapacity():
    return ok(0.0)


@app.route('/api/v1/camera/0/maxadu', methods=['GET'])
def maxadu():
    return ok(255)


@app.route('/api/v1/camera/0/canpulseguide', methods=['GET'])
def canpulseguide():
    return ok(False)


@app.route('/api/v1/camera/0/ispulseguiding', methods=['GET'])
def ispulseguiding():
    return ok(False)


@app.route('/api/v1/camera/0/pulseguide', methods=['PUT'])
def pulseguide():
    return ascom_error(1024, "Not implemented -- CanPulseGuide is False")


@app.route('/api/v1/camera/0/readoutmodes', methods=['GET'])
def readoutmodes():
    return ok(["Default"])


@app.route('/api/v1/camera/0/readoutmode', methods=['GET', 'PUT'])
def readoutmode():
    if request.method == 'PUT':
        get_required_int('ReadoutMode')  # validated, but only one mode exists
        return ok()
    return ok(0)


@app.route('/api/v1/camera/0/gains', methods=['GET'])
def gains():
    return ascom_error(1024, "Not implemented")


@app.route('/api/v1/camera/0/offsets', methods=['GET'])
def offsets():
    return ascom_error(1024, "Not implemented -- driver is in Offset Value mode")


@app.route('/api/v1/camera/0/setccdtemperature', methods=['GET', 'PUT'])
def setccdtemperature():
    if request.method == 'PUT':
        get_required_float('SetCCDTemperature')
    return ascom_error(1024, "Not implemented -- CanSetCCDTemperature is False")


@app.route('/api/v1/camera/0/subexposureduration', methods=['GET', 'PUT'])
def subexposureduration():
    if request.method == 'PUT':
        get_required_float('SubExposureDuration')
    return ascom_error(1024, "Not implemented -- no sub-exposure/HDR stacking mode on this camera")


@app.route('/api/v1/camera/0/exposureresolution', methods=['GET'])
def exposureresolution():
    return ok(0.0)


@app.route('/api/v1/camera/0/percentcompleted', methods=['GET'])
def percentcompleted():
    with state_lock:
        return ok(100 if image_ready else 0)


@app.route('/api/v1/camera/0/lastexposureduration', methods=['GET'])
def lastexposureduration():
    with state_lock:
        if not has_exposed:
            return ascom_error(1025, "No exposure has been taken yet")
        return ok(last_actual_exposure_s)


@app.route('/api/v1/camera/0/lastexposurestarttime', methods=['GET'])
def lastexposurestarttime():
    with state_lock:
        if not has_exposed:
            return ascom_error(1025, "No exposure has been taken yet")
    return ok(time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()))


@app.route('/api/v1/camera/0/cameraxsize', methods=['GET'])
def cameraxsize():
    return ok(SENSOR_MAX_WIDTH)


@app.route('/api/v1/camera/0/cameraysize', methods=['GET'])
def cameraysize():
    return ok(SENSOR_MAX_HEIGHT)


@app.route('/api/v1/camera/0/pixelsizex', methods=['GET'])
def pixelsizex():
    return ok(PIXEL_SIZE_UM)


@app.route('/api/v1/camera/0/pixelsizey', methods=['GET'])
def pixelsizey():
    return ok(PIXEL_SIZE_UM)


@app.route('/api/v1/camera/0/maxbinx', methods=['GET'])
def maxbinx():
    return ok(binning_max_x)


@app.route('/api/v1/camera/0/maxbiny', methods=['GET'])
def maxbiny():
    return ok(binning_max_y)


@app.route('/api/v1/camera/0/binx', methods=['GET', 'PUT'])
def binx():
    global bin_x
    if request.method == 'PUT':
        val = get_required_int('BinX')
        if val < 1 or val > binning_max_x:
            return ascom_error(1025, f"BinX must be between 1 and {binning_max_x}")
        bin_x = val
        return ok()
    return ok(bin_x)


@app.route('/api/v1/camera/0/biny', methods=['GET', 'PUT'])
def biny():
    global bin_y
    if request.method == 'PUT':
        val = get_required_int('BinY')
        if val < 1 or val > binning_max_y:
            return ascom_error(1025, f"BinY must be between 1 and {binning_max_y}")
        bin_y = val
        return ok()
    return ok(bin_y)


@app.route('/api/v1/camera/0/decimationx', methods=['GET', 'PUT'])
def decimationx():
    global current_decimation_x
    if request.method == 'PUT':
        if not HAS_DECIMATION:
            return ascom_error(1024, "Not implemented -- no subsampling node on this camera")
        val = get_required_int('DecimationX')
        if val < 1 or val > decimation_max_x:
            return ascom_error(1025, f"DecimationX must be between 1 and {decimation_max_x}")
        current_decimation_x = val
        return ok()
    return ok(current_decimation_x)


@app.route('/api/v1/camera/0/decimationy', methods=['GET', 'PUT'])
def decimationy():
    global current_decimation_y
    if request.method == 'PUT':
        if not HAS_DECIMATION:
            return ascom_error(1024, "Not implemented -- no subsampling node on this camera")
        val = get_required_int('DecimationY')
        if val < 1 or val > decimation_max_y:
            return ascom_error(1025, f"DecimationY must be between 1 and {decimation_max_y}")
        current_decimation_y = val
        return ok()
    return ok(current_decimation_y)


@app.route('/api/v1/camera/0/startx', methods=['GET', 'PUT'])
def startx():
    global aoi_start_x
    if request.method == 'PUT':
        aoi_start_x = get_required_int('StartX')
        return ok()
    return ok(aoi_start_x)


@app.route('/api/v1/camera/0/starty', methods=['GET', 'PUT'])
def starty():
    global aoi_start_y
    if request.method == 'PUT':
        aoi_start_y = get_required_int('StartY')
        return ok()
    return ok(aoi_start_y)


@app.route('/api/v1/camera/0/numx', methods=['GET', 'PUT'])
def numx():
    global aoi_num_x
    if request.method == 'PUT':
        aoi_num_x = get_required_int('NumX')
        return ok()
    return ok(aoi_num_x)


@app.route('/api/v1/camera/0/numy', methods=['GET', 'PUT'])
def numy():
    global aoi_num_y
    if request.method == 'PUT':
        aoi_num_y = get_required_int('NumY')
        return ok()
    return ok(aoi_num_y)


@app.route('/api/v1/camera/0/gain', methods=['GET', 'PUT'])
def gain():
    global current_gain
    if request.method == 'PUT':
        val = get_required_int('Gain')
        if val < real_gain_min or val > real_gain_max:
            return ascom_error(1025, f"Gain must be between {real_gain_min} and {real_gain_max}")
        current_gain = val
        return ok()
    return ok(current_gain)


@app.route('/api/v1/camera/0/gainmin', methods=['GET'])
def gainmin():
    return ok(real_gain_min)


@app.route('/api/v1/camera/0/gainmax', methods=['GET'])
def gainmax():
    return ok(real_gain_max)


@app.route('/api/v1/camera/0/offset', methods=['GET', 'PUT'])
def offset():
    global current_offset
    if request.method == 'PUT':
        val = get_required_int('Offset')
        if val < real_offset_min or val > real_offset_max:
            return ascom_error(1025, f"Offset must be between {real_offset_min} and {real_offset_max}")
        current_offset = val
        return ok()
    return ok(current_offset)


@app.route('/api/v1/camera/0/offsetmin', methods=['GET'])
def offsetmin():
    return ok(real_offset_min)


@app.route('/api/v1/camera/0/offsetmax', methods=['GET'])
def offsetmax():
    return ok(real_offset_max)


@app.route('/api/v1/camera/0/exposuremin', methods=['GET'])
def exposuremin():
    return ok(0.0001)


@app.route('/api/v1/camera/0/exposuremax', methods=['GET'])
def exposuremax():
    # Reports the REAL current ceiling (whatever UserSet0/Default has
    # loaded), not a hardcoded 60s -- a hardcoded value here would mislead
    # ASCOM clients into requesting exposures the camera can't actually
    # deliver in its current state.
    try:
        return ok(float(node_map.ExposureTime.max) / 1_000_000.0)
    except Exception:
        return ok(60.0)


@app.route('/api/v1/camera/0/startexposure', methods=['PUT'])
def start_exposure():
    global current_exposure_s
    requested_duration = get_required_float('Duration')
    get_required_bool('Light')  # validated for conformance; monochrome/color both treated as light frame
    if requested_duration < 0:
        return ascom_error(1025, "Duration cannot be negative")
    current_exposure_s = requested_duration
    with state_lock:
        if camera_state in (2, 3):
            return ascom_error(1026, "Camera busy")
    try:
        apply_aoi()
    except Exception as e:
        print(f"[-] apply_aoi: {type(e).__name__}: {e}")
        return ascom_error(1025, f"AOI failed: {e}")
    threading.Thread(target=do_exposure, args=(current_exposure_s,), daemon=True).start()
    return ok()


@app.route('/api/v1/camera/0/stopexposure', methods=['PUT'])
def stop_exposure():
    return ok()


@app.route('/api/v1/camera/0/abortexposure', methods=['PUT'])
def abort_exposure():
    global camera_state
    with state_lock:
        camera_state = 0
    return ok()


@app.route('/api/v1/camera/0/camerastate', methods=['GET'])
def camerastate():
    with state_lock:
        return ok(camera_state)


@app.route('/api/v1/camera/0/imageready', methods=['GET'])
def imageready():
    with state_lock:
        return ok(image_ready)


@app.route('/api/v1/camera/0/imagearray', methods=['GET'])
def imagearray():
    with state_lock:
        if not image_ready or last_image is None:
            return ascom_error(1, "No image yet")
        frame = last_image
    cid, ctid = alpaca_ids()
    return jsonify({"Type": 2, "Rank": 2, "Value": frame.astype(int).T.tolist(),
                     "ErrorNumber": 0, "ErrorMessage": "",
                     "ClientTransactionID": ctid, "ServerTransactionID": next_server_transaction_id()})


@app.route('/api/v1/camera/0/imagearrayvariant', methods=['GET'])
def imagearrayvariant():
    return imagearray()


@app.route('/api/v1/camera/0/imagebytes', methods=['GET'])
def imagebytes():
    with state_lock:
        if not image_ready or last_image is None:
            return jsonify({"ErrorNumber": 1, "ErrorMessage": "No image"}), 400
        frame = last_image
    data = frame.astype(np.int32)
    h, w = data.shape
    header = struct.pack('<iiIIiiiiiii', 1, 0, 0, 1, 44, 2, 2, 2, w, h, 0)
    return Response(header + data.tobytes(order='C'), mimetype='application/imagebytes')


def start_alpaca_discovery_responder(alpaca_port=11111, discovery_port=32227):
    import socket
    import json

    def listen():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', discovery_port))
        print(f"[*] Alpaca discovery responder listening on UDP {discovery_port}")
        while True:
            try:
                data, addr = sock.recvfrom(1024)
                message = data.decode('ascii', errors='ignore').strip()
                if message.startswith("alpacadiscovery1"):
                    response = json.dumps({"AlpacaPort": alpaca_port}).encode('ascii')
                    sock.sendto(response, addr)
            except Exception as e:
                print(f"[-] Discovery responder error: {e}")

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    return t


if __name__ == '__main__':
    start_alpaca_discovery_responder(alpaca_port=11111)
    try:
        app.run(host='127.0.0.1', port=11111, threaded=True)
    except KeyboardInterrupt:
        print("\n[*] Ctrl+C received -- shutting down...")
    finally:
        print("[*] Releasing camera...")
        try:
            ia.stop()
        except Exception:
            pass
        try:
            ia.destroy()
        except Exception:
            pass
        try:
            harvester.reset()
        except Exception:
            pass
        print("[*] Done.")
        os._exit(0)