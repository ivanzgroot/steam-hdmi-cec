"""The DisplayPort tunneling fix: finding the AUX device, and the enable bit.

This is the part of the project that must not regress. Everything else can fail
loudly; this one fails by looking healthy - the adapter enumerates, the physical
address reads back correctly, and every directed frame is silently NACKed.

The lookups are tested against a fake sysfs tree because the three fallbacks
exist for three different kernel layouts, and the only way to know all three
still work is to build all three.
"""

import os
import sys
import tempfile

from _harness import Checks

import cec_dpcd

check = Checks()


def sysfs(layout):
    """Build a throwaway /sys tree. layout maps relative path -> contents, or
    None for a directory."""
    root = tempfile.mkdtemp()
    for relative, contents in layout.items():
        path = os.path.join(root, relative.replace("/", os.sep))
        if contents is None:
            os.makedirs(path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(contents)
    return root


check.section("the register address is the one from the DisplayPort spec")
check("DP_CEC_TUNNELING_CONTROL", cec_dpcd.DPCD_CEC_TUNNELING_CONTROL, 0x3001)
check("the enable value", cec_dpcd.DPCD_CEC_TUNNELING_ENABLE, 0x01)

check.section("finding the DRM connector for a CEC adapter")
drm = sysfs({
    "card0-DP-1/status": "connected\n",
    "card0-HDMI-A-1/status": "disconnected\n",
    "card1-DP-2/status": "connected\n",
})
check("the adapter name selects the connector",
      os.path.basename(cec_dpcd.find_connector("DP-1", drm_dir=drm)), "card0-DP-1")
check("a different adapter finds a different connector",
      os.path.basename(cec_dpcd.find_connector("DP-2", drm_dir=drm)), "card1-DP-2")
check("an HDMI connector is found too",
      os.path.basename(cec_dpcd.find_connector("HDMI-A-1", drm_dir=drm)), "card0-HDMI-A-1")
check("an unknown adapter finds nothing",
      cec_dpcd.find_connector("DP-9", drm_dir=drm), None)
check("an empty adapter name finds nothing",
      cec_dpcd.find_connector("", drm_dir=drm), None)
check("a missing sysfs is not fatal",
      cec_dpcd.find_connector("DP-1", drm_dir=os.path.join(drm, "nope")), None)

# "DP-1" must not match "card0-DP-11". The suffix is anchored on the dash.
drm = sysfs({"card0-DP-11/status": "connected\n"})
check("a longer connector name is not a partial match",
      cec_dpcd.find_connector("DP-1", drm_dir=drm), None)

check.section("finding the AUX device: route 1, inside the connector")
drm = sysfs({
    "card0-DP-1/status": "connected\n",
    "card0-DP-1/drm_dp_aux3": None,
})
connector = cec_dpcd.find_connector("DP-1", drm_dir=drm)
check("the aux node under the connector wins",
      cec_dpcd.find_aux_device(connector, aux_class_dir=os.path.join(drm, "none")),
      "/dev/drm_dp_aux3")

check.section("route 2, the drm_dp_aux_dev class directory")
drm = sysfs({"card0-DP-1/status": "connected\n"})
aux_class = sysfs({"drm_dp_aux2/device": "", "drm_dp_aux7/device": ""})
# The class entries point back at their connector through a "device" symlink;
# realpath on a plain file still contains the name, which is what the lookup
# matches on.
os.remove(os.path.join(aux_class, "drm_dp_aux7", "device"))
os.makedirs(os.path.join(aux_class, "drm_dp_aux7", "card0-DP-1"), exist_ok=True)
try:
    os.symlink(os.path.join(aux_class, "drm_dp_aux7", "card0-DP-1"),
               os.path.join(aux_class, "drm_dp_aux7", "device"))
    symlinks_work = True
except (OSError, NotImplementedError):
    # Windows without developer mode. The route-2 lookup is Linux-only anyway.
    symlinks_work = False

connector = cec_dpcd.find_connector("DP-1", drm_dir=drm)
if symlinks_work:
    check("the class entry pointing at our connector is chosen",
          cec_dpcd.find_aux_device(connector, aux_class_dir=aux_class),
          "/dev/drm_dp_aux7")
else:
    print("  skip route-2 lookup (symlinks unavailable on this host)")

check.section("route 3, the single-output fallback")
# Deliberately blunt, and kept because it is right on any machine with one
# DisplayPort output - which is the machine this project runs on. Dropping it
# would trade a working fix for tidiness.
drm = sysfs({"card0-DP-1/status": "connected\n"})
connector = cec_dpcd.find_connector("DP-1", drm_dir=drm)
found = cec_dpcd.find_aux_device(connector, aux_class_dir=os.path.join(drm, "none"))
check("falls back to aux0 when it exists, else None",
      found in ("/dev/drm_dp_aux0", None), True)

check.section("a plain HDMI adapter has nothing to fix, and must not break")
tunneling = cec_dpcd.DpcdTunneling.__new__(cec_dpcd.DpcdTunneling)
tunneling.log = lambda _m: None
tunneling.connector = None
tunneling.aux_path = None
check("it reports itself unavailable", tunneling.available, False)
check("ensure_enabled succeeds anyway", tunneling.ensure_enabled(), True)
check("status explains itself", "no DisplayPort AUX" in tunneling.status()[1], True)
check("a re-probe with no connector is a no-op", tunneling.reprobe_connector(), False)

if not hasattr(os, "pread"):
    print("\n  skip register read/write (os.pread is Unix-only; "
          "the logic above is what matters off Linux)")
else:
    check.section("reading and writing the register")
    scratch = tempfile.mkdtemp()
    aux = os.path.join(scratch, "drm_dp_aux0")
    with open(aux, "wb") as handle:
        handle.write(b"\0" * (cec_dpcd.DPCD_CEC_TUNNELING_CONTROL + 16))

    check("a cleared register reads as 0", cec_dpcd.read_tunneling(aux), 0)
    check("the write succeeds", cec_dpcd.write_tunneling(aux), True)
    check("and it reads back as enabled", cec_dpcd.read_tunneling(aux), 1)

    # The byte must land at 0x3001 exactly - one byte out is a different
    # register, and writing a stray one is how you brick a link.
    with open(aux, "rb") as handle:
        blob = handle.read()
    check("only one byte changed", blob.count(b"\x01"), 1)
    check("and it is at 0x3001", blob[cec_dpcd.DPCD_CEC_TUNNELING_CONTROL], 1)
    check("the byte before is untouched",
          blob[cec_dpcd.DPCD_CEC_TUNNELING_CONTROL - 1], 0)
    check("the byte after is untouched",
          blob[cec_dpcd.DPCD_CEC_TUNNELING_CONTROL + 1], 0)

    check("an unreadable path returns None rather than raising",
          cec_dpcd.read_tunneling(os.path.join(scratch, "nope")), None)
    check("an unwritable path returns False rather than raising",
          cec_dpcd.write_tunneling(os.path.join(scratch, "nope", "deeper")), False)

    check.section("ensure_enabled only writes when it has to")
    lines = []
    tunneling = cec_dpcd.DpcdTunneling.__new__(cec_dpcd.DpcdTunneling)
    tunneling.log = lines.append
    tunneling.connector = None
    tunneling.aux_path = aux

    check("an already-enabled register succeeds", tunneling.ensure_enabled(), True)
    check("and says nothing about re-enabling", lines, [])

    cec_dpcd.write_tunneling(aux, 0)
    check("a cleared register is re-enabled", tunneling.ensure_enabled(), True)
    check("and it is logged", any("re-enabling" in line for line in lines), True)
    check("the register is set afterwards", cec_dpcd.read_tunneling(aux), 1)

sys.exit(check.finish())
