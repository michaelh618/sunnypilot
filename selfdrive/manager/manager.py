#!/usr/bin/env python3
import datetime
import os
import signal
import subprocess
import sys
import traceback
from typing import List, Tuple, Union

import cereal.messaging as messaging
import selfdrive.sentry as sentry
from common.basedir import BASEDIR
from common.params import Params, ParamKeyType
from common.text_window import TextWindow
from selfdrive.boardd.set_time import set_time
from system.hardware import HARDWARE, PC
from selfdrive.manager.helpers import unblock_stdout
from selfdrive.manager.process import ensure_running
from selfdrive.manager.process_config import managed_processes
from selfdrive.sentry import CRASHES_DIR
from selfdrive.athena.registration import register, UNREGISTERED_DONGLE_ID
from system.swaglog import cloudlog, add_file_handler
from system.version import is_dirty, get_commit, get_version, get_origin, get_short_branch, \
                              terms_version, training_version, is_tested_branch


sys.path.append(os.path.join(BASEDIR, "pyextra"))


def manager_init() -> None:
  # update system time from panda
  set_time(cloudlog)

  # save boot log
  subprocess.call("./bootlog", cwd=os.path.join(BASEDIR, "selfdrive/loggerd"))

  params = Params()
  params.clear_all(ParamKeyType.CLEAR_ON_MANAGER_START)

  default_params: List[Tuple[str, Union[str, bytes]]] = [
    ("AccMadsCombo", "1"),
    ("AutoLaneChangeTimer", "0"),
    ("BelowSpeedPause", "0"),
    ("BrakeLights", "0"),
    ("BrightnessControl", "0"),
    ("CustomTorqueLateral", "0"),
    ("CameraControl", "2"),
    ("CameraControlToggle", "0"),
    ("CameraOffset", "0"),
    ("CarModel", ""),
    ("CarModelText", ""),
    ("ChevronInfo", "1"),
    ("CustomBootScreen", "0"),
    ("CustomOffsets", "0"),
    ("CompletedTrainingVersion", "0"),
    ("DevUI", "1"),
    ("DevUIRow", "1"),
    ("DisableOnroadUploads", "0"),
    ("DisengageLateralOnBrake", "1"),
    ("DisengageOnAccelerator", "0"),
    ("DynamicLaneProfile", "2"),
    ("DynamicLaneProfileToggle", "1"),
    ("EnableMads", "1"),
    ("EndToEndLongToggle", "1"),
    ("EnhancedScc", "0"),
    ("GapAdjustCruise", "1"),
    ("GapAdjustCruiseMode", "0"),
    ("GapAdjustCruiseTr", "4"),
    ("GpxDeleteAfterUpload", "1"),
    ("GpxDeleteIfUploaded", "1"),
    ("GsmMetered", "1"),
    ("HandsOnWheelMonitoring", "0"),
    ("HasAcceptedTerms", "0"),
    ("LanguageSetting", "main_en"),
    ("LastSpeedLimitSignTap", "0"),
    ("MadsIconToggle", "1"),
    ("MaxTimeOffroad", "9"),
    ("OnroadScreenOff", "0"),
    ("OnroadScreenOffBrightness", "50"),
    ("OpenpilotEnabledToggle", "1"),
    ("PathOffset", "0"),
    ("ReverseAccChange", "0"),
    ("ShowDebugUI", "1"),
    ("SpeedLimitControl", "1"),
    ("SpeedLimitPercOffset", "1"),
    ("SpeedLimitStyle", "0"),
    ("SpeedLimitValueOffset", "0"),
    ("StandStillTimer", "0"),
    ("StockLongToyota", "0"),
    ("TorqueDeadzoneDeg", "0"),
    ("TorqueFriction", "1"),
    ("TorqueMaxLatAccel", "250"),
    ("TurnSpeedControl", "0"),
    ("TurnVisionControl", "0"),
    ("VisionCurveLaneless", "0"),
    ("VwAccType", "0"),
  ]
  if not PC:
    default_params.append(("LastUpdateTime", datetime.datetime.utcnow().isoformat().encode('utf8')))

  if params.get_bool("RecordFrontLock"):
    params.put_bool("RecordFront", True)

  # set unset params
  for k, v in default_params:
    if params.get(k) is None:
      params.put(k, v)

  # is this dashcam?
  if os.getenv("PASSIVE") is not None:
    params.put_bool("Passive", bool(int(os.getenv("PASSIVE", "0"))))

  if params.get("Passive") is None:
    raise Exception("Passive must be set to continue")

  # Create folders needed for msgq
  try:
    os.mkdir("/dev/shm")
  except FileExistsError:
    pass
  except PermissionError:
    print("WARNING: failed to make /dev/shm")

  # set version params
  params.put("Version", get_version())
  params.put("TermsVersion", terms_version)
  params.put("TrainingVersion", training_version)
  params.put("GitCommit", get_commit(default=""))
  params.put("GitBranch", get_short_branch(default=""))
  params.put("GitRemote", get_origin(default=""))
  params.put_bool("IsTestedBranch", is_tested_branch())

  # set dongle id
  reg_res = register(show_spinner=True)
  if reg_res:
    dongle_id = reg_res
  else:
    serial = params.get("HardwareSerial")
    raise Exception(f"Registration failed for device {serial}")
  os.environ['DONGLE_ID'] = dongle_id  # Needed for swaglog

  if not is_dirty():
    os.environ['CLEAN'] = '1'

  # init logging
  sentry.init(sentry.SentryProject.SELFDRIVE)
  cloudlog.bind_global(dongle_id=dongle_id, version=get_version(), dirty=is_dirty(),
                       device=HARDWARE.get_device_type())

  if os.path.isfile(f'{CRASHES_DIR}/error.txt'):
    os.remove(f'{CRASHES_DIR}/error.txt')


def manager_prepare() -> None:
  for p in managed_processes.values():
    p.prepare()


def manager_cleanup() -> None:
  # send signals to kill all procs
  for p in managed_processes.values():
    p.stop(block=False)

  # ensure all are killed
  for p in managed_processes.values():
    p.stop(block=True)

  cloudlog.info("everything is dead")


def manager_thread() -> None:
  cloudlog.bind(daemon="manager")
  cloudlog.info("manager start")
  cloudlog.info({"environ": os.environ})

  params = Params()

  ignore: List[str] = []
  if params.get("DongleId", encoding='utf8') in (None, UNREGISTERED_DONGLE_ID):
    ignore += ["manage_athenad", "uploader"]
  if os.getenv("NOBOARD") is not None:
    ignore.append("pandad")
  ignore += [x for x in os.getenv("BLOCK", "").split(",") if len(x) > 0]

  sm = messaging.SubMaster(['deviceState', 'carParams'], poll=['deviceState'])
  pm = messaging.PubMaster(['managerState'])

  ensure_running(managed_processes.values(), False, params=params, CP=sm['carParams'], not_run=ignore)

  while True:
    sm.update()

    started = sm['deviceState'].started
    ensure_running(managed_processes.values(), started, params=params, CP=sm['carParams'], not_run=ignore)

    running = ' '.join("%s%s\u001b[0m" % ("\u001b[32m" if p.proc.is_alive() else "\u001b[31m", p.name)
                       for p in managed_processes.values() if p.proc)
    print(running)
    cloudlog.debug(running)

    # send managerState
    msg = messaging.new_message('managerState')
    msg.managerState.processes = [p.get_process_state_msg() for p in managed_processes.values()]
    pm.send('managerState', msg)

    # Exit main loop when uninstall/shutdown/reboot is needed
    shutdown = False
    for param in ("DoUninstall", "DoShutdown", "DoReboot"):
      if params.get_bool(param):
        shutdown = True
        params.put("LastManagerExitReason", param)
        cloudlog.warning(f"Shutting down manager - {param} set")

    if shutdown:
      break


def main() -> None:
  prepare_only = os.getenv("PREPAREONLY") is not None

  manager_init()

  # Start UI early so prepare can happen in the background
  if not prepare_only:
    managed_processes['ui'].start()

  manager_prepare()

  if prepare_only:
    return

  # SystemExit on sigterm
  signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(1))

  try:
    manager_thread()
  except Exception:
    traceback.print_exc()
    sentry.capture_exception()
  finally:
    manager_cleanup()

  params = Params()
  if params.get_bool("DoUninstall"):
    cloudlog.warning("uninstalling")
    HARDWARE.uninstall()
  elif params.get_bool("DoReboot"):
    cloudlog.warning("reboot")
    HARDWARE.reboot()
  elif params.get_bool("DoShutdown"):
    cloudlog.warning("shutdown")
    HARDWARE.shutdown()


if __name__ == "__main__":
  unblock_stdout()

  try:
    main()
  except Exception:
    add_file_handler(cloudlog)
    cloudlog.exception("Manager failed to start")

    try:
      managed_processes['ui'].stop()
    except Exception:
      pass

    # Show last 3 lines of traceback
    error = traceback.format_exc(-3)
    error = "Manager failed to start\n\n" + error
    with TextWindow(error) as t:
      t.wait_for_exit()

    raise

  # manual exit because we are forked
  sys.exit(0)
