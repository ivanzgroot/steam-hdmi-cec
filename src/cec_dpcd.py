"""The DisplayPort fix this whole project exists for.

The CEC controller is not in the PC. It lives inside the DP->HDMI adapter and is
driven over the DisplayPort AUX channel - "CEC tunneling over AUX". When the
machine suspends, the adapter loses power and its CEC block resets, clearing
DPCD register 0x3001 (DP_CEC_TUNNELING_CONTROL). The kernel never rewrites it,
because the EDID has not changed and from its point of view nothing needs
reconfiguring.

The result is a failure that looks exactly like working hardware. The physical
address still reads back correctly, the adapter still enumerates, every status
looks healthy - and every directed CEC frame is silently NACKed.

Writing the enable bit back is the software equivalent of reseating the HDMI
cable, and it is the difference between this project working after a suspend and
not. Two rules protect it:

  1. It is applied before every wake attempt, not once at startup.
  2. It is applied AFTER any logical-address reconfiguration, never before -
     claiming a logical address resets the adapter and clears the bit again.
     Getting this order wrong produces a wake that works from a cold boot and
     fails after resume, which is the exact bug this file was written to kill.

tests/test_dpcd.py pins both.
"""

import os

DPCD_CEC_TUNNELING_CONTROL = 0x3001
DPCD_CEC_TUNNELING_ENABLE = 0x01

DRM_DIR = "/sys/class/drm"
AUX_CLASS_DIR = "/sys/class/drm_dp_aux_dev"


class DpcdUnavailable(Exception):
    """No DP AUX device could be found - the adapter is not a DisplayPort one,
    or sysfs is laid out unexpectedly. Never fatal: a direct HDMI port has no
    tunneling bit to fix and works fine without one."""


def find_connector(adapter_name, drm_dir=DRM_DIR):
    """The DRM connector sysfs directory backing a CEC adapter.

    The CEC adapter reports its name (e.g. "DP-1") through CEC_ADAP_G_CAPS, and
    the connector directory is card<N>-<name>.
    """
    if not adapter_name:
        return None
    try:
        entries = sorted(os.listdir(drm_dir))
    except OSError:
        return None
    suffix = "-" + adapter_name
    for entry in entries:
        if entry.startswith("card") and entry.endswith(suffix):
            path = os.path.join(drm_dir, entry)
            if os.path.exists(os.path.join(path, "status")):
                return path
    return None


def find_aux_device(connector, aux_class_dir=AUX_CLASS_DIR):
    """The /dev/drm_dp_auxN node belonging to a connector.

    Three routes, tried in order, because which of them exists depends on the
    kernel version and the driver. The last one is a blunt guess that is right
    on any machine with a single DisplayPort output, which is the common case
    here - keeping it means a working fix on setups where the tidy lookups
    happen to come up empty.
    """
    if connector:
        try:
            for entry in sorted(os.listdir(connector)):
                if entry.startswith("drm_dp_aux"):
                    return "/dev/" + entry
        except OSError:
            pass

        name = os.path.basename(connector)
        try:
            for entry in sorted(os.listdir(aux_class_dir)):
                if not entry.startswith("drm_dp_aux"):
                    continue
                link = os.path.join(aux_class_dir, entry, "device")
                try:
                    target = os.path.realpath(link)
                except OSError:
                    continue
                if name in target:
                    return "/dev/" + entry
        except OSError:
            pass

    if os.path.exists("/dev/drm_dp_aux0"):
        return "/dev/drm_dp_aux0"
    return None


def read_tunneling(aux_path):
    """The current value of DPCD 0x3001, or None if it cannot be read.

    The AUX character device maps file offsets straight onto DPCD addresses, so
    this is a one-byte read at 0x3001 - the whole of what the old shell version
    needed dd, od and tr to do.
    """
    try:
        fd = os.open(aux_path, os.O_RDONLY)
    except OSError:
        return None
    try:
        data = os.pread(fd, 1, DPCD_CEC_TUNNELING_CONTROL)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data[0] if data else None


def write_tunneling(aux_path, value=DPCD_CEC_TUNNELING_ENABLE):
    try:
        fd = os.open(aux_path, os.O_WRONLY)
    except OSError:
        return False
    try:
        return os.pwrite(fd, bytes([value]), DPCD_CEC_TUNNELING_CONTROL) == 1
    except OSError:
        return False
    finally:
        os.close(fd)


class DpcdTunneling:
    """The tunneling bit for one adapter, with logging.

    Constructed from a CecDevice's adapter name. If no AUX device is found this
    object stays usable and every method reports "not applicable" rather than
    failing - a plain HDMI adapter has no tunneling bit and needs none.
    """

    def __init__(self, adapter_name, log=None, drm_dir=DRM_DIR,
                 aux_class_dir=AUX_CLASS_DIR):
        self.log = log or (lambda _message: None)
        self.connector = find_connector(adapter_name, drm_dir=drm_dir)
        self.aux_path = find_aux_device(self.connector, aux_class_dir=aux_class_dir)

    @property
    def available(self):
        return self.aux_path is not None

    def status(self):
        """(value, human-readable text). Read-only; used by "cec-hook status"."""
        if not self.available:
            return None, "no DisplayPort AUX device (direct HDMI adapter?)"
        value = read_tunneling(self.aux_path)
        if value is None:
            return None, "%s  0x3001 unreadable (run as root?)" % self.aux_path
        if value == DPCD_CEC_TUNNELING_ENABLE:
            return value, "%s  0x3001 = 0x01  CEC tunneling enabled" % self.aux_path
        return value, ("%s  0x3001 = 0x%02X  CEC tunneling DISABLED "
                       "(the next wake will fix it)" % (self.aux_path, value))

    def ensure_enabled(self):
        """Enable the tunneling bit if it is not already set.

        Returns True when tunneling is on afterwards. Called before every wake
        attempt, and again after anything that reconfigures the adapter.
        """
        if not self.available:
            return True

        value = read_tunneling(self.aux_path)
        if value == DPCD_CEC_TUNNELING_ENABLE:
            return True

        self.log("CEC tunneling is off on %s (0x3001=0x%s), re-enabling"
                 % (self.aux_path, "%02X" % value if value is not None else "??"))
        if not write_tunneling(self.aux_path):
            self.log("WARNING: could not write 0x3001 on %s" % self.aux_path)
            return False

        value = read_tunneling(self.aux_path)
        if value == DPCD_CEC_TUNNELING_ENABLE:
            self.log("CEC tunneling re-enabled")
            return True
        self.log("WARNING: 0x3001 did not stick (reads back 0x%s)"
                 % ("%02X" % value if value is not None else "??"))
        return False

    def reprobe_connector(self):
        """Force the DRM connector to re-detect: the software cable reseat.

        This makes the kernel re-read the EDID and fully re-register the CEC
        adapter. It is the heaviest thing this project does and it can blank the
        display for a second or two, so it is only reached after the cheaper
        recoveries have failed.
        """
        if not self.connector:
            self.log("WARNING: no DRM connector found, skipping re-probe")
            return False

        status_path = os.path.join(self.connector, "status")
        self.log("forcing a DRM re-probe on %s (software cable reseat)"
                 % os.path.basename(self.connector))
        for value in ("off", "detect"):
            try:
                with open(status_path, "w") as handle:
                    handle.write(value)
            except OSError as exc:
                self.log("  ERROR: writing '%s' to %s failed: %s"
                         % (value, status_path, exc))
                return False
        return True

    def connector_status(self):
        if not self.connector:
            return "unknown"
        try:
            with open(os.path.join(self.connector, "status")) as handle:
                return handle.read().strip()
        except OSError:
            return "unknown"
