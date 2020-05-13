# coding=utf-8
from datetime import datetime
import time
import random
import logging
import raven
import re
import os
import platform
from sarge import run, Capture
import tempfile
from io import BytesIO
import struct


CAM_EXCLUSIVE_USE = os.path.join(tempfile.gettempdir(), '.using_picam')

_logger = logging.getLogger('octoprint.plugins.thespaghettidetective')

class ExpoBackoff:

    def __init__(self, max_seconds):
        self.attempts = -3
        self.max_seconds = max_seconds

    def reset(self):
        self.attempts = -3

    def more(self, e):
        self.attempts += 1
        delay = 2 ** self.attempts
        if delay > self.max_seconds:
            delay = self.max_seconds
        delay *= 0.5 + random.random()

        _logger.error('Backing off %f seconds: %s' % (delay, e))

        time.sleep(delay)


class ConnectionErrorTracker:

    def __init__(self, plugin):
        self.plugin = plugin
        self.attempts = dict()
        self.errors = dict()

    def attempt(self, error_type):
        attempts = self.attempts.get(error_type, 0)
        self.attempts[error_type] = attempts + 1

    def add_connection_error(self, error_type):
        existing = self.errors.get(error_type, [])
        self.errors[error_type] = existing + [datetime.utcnow()]
        self.notify_client_if_needed_for_error(error_type)

    def notify_client_if_needed_for_error(self, error_type):
        attempts = self.attempts.get(error_type, 0)
        errors = self.errors.get(error_type, [])

        if attempts < 8:
            return

        if attempts < 10 and len(errors) < attempts * 0.5:
            return

        if len(errors) < attempts * 0.25:
            return

        self.plugin._plugin_manager.send_plugin_message(self.plugin._identifier, {'new_error': error_type})

    def as_dict(self):
        return self.errors

class SentryWrapper:

    def __init__(self, plugin):
        self.sentryClient = raven.Client(
            'https://f0356e1461124e69909600a64c361b71:bdf215f6e71b48dc90d28fb89a4f8238@sentry.thespaghettidetective.com/4?verify_ssl=0',
            release=plugin._plugin_version
            )
        self.plugin = plugin

    def captureException(self, *args, **kwargs):
        _logger.exception("Exception")
        if self.plugin._settings.get(["sentry_opt"]) != 'out':
            self.sentryClient.captureException(*args, **kwargs)

    def user_context(self, *args, **kwargs):
        if self.plugin._settings.get(["sentry_opt"]) != 'out':
            self.sentryClient.user_context(*args, **kwargs)

    def captureMessage(self, *args, **kwargs):
        if self.plugin._settings.get(["sentry_opt"]) != 'out':
            self.sentryClient.captureMessage(*args, **kwargs)


def pi_version():
    try:
        with open('/sys/firmware/devicetree/base/model', 'r') as firmware_model:
            model = re.search('Raspberry Pi(.*)', firmware_model.read()).group(1)
            if model:
                return "0" if re.search('Zero', model, re.IGNORECASE) else "3"
            else:
                return None
    except:
         return None

system_tags = None

def get_tags():
    global system_tags
    if system_tags:
        return system_tags

    (os, _, ver, _, arch, _) = platform.uname()
    tags = dict(os=os, os_ver=ver, arch=arch)
    try:
        v4l2 = run('v4l2-ctl --list-devices',stdout=Capture())
        v4l2_out = ''.join(re.compile(r"^([^\t]+)", re.MULTILINE).findall(v4l2.stdout.text)).replace('\n', '')
        if v4l2_out:
            tags['v4l2'] = v4l2_out
    except:
        pass

    try:
        usb = run("lsusb | cut -d ' ' -f 7- | grep -vE ' hub| Hub' | grep -v 'Standard Microsystems Corp'",stdout=Capture())
        usb_out = ''.join(usb.stdout.text).replace('\n', '')
        if usb_out:
            tags['usb'] = usb_out
    except:
        pass

    system_tags = tags
    return system_tags

def not_using_pi_camera():
    try:
        os.remove(CAM_EXCLUSIVE_USE)
    except:
        pass

def using_pi_camera():
    open(CAM_EXCLUSIVE_USE, 'a').close()  # touch CAM_EXCLUSIVE_USE to indicate the intention of exclusive use of pi camera


def get_image_info(data):
    data_bytes = data
    if not isinstance(data, str):
        data = data.decode('iso-8859-1')
    size = len(data)
    height = -1
    width = -1
    content_type = ''

    # handle GIFs
    if (size >= 10) and data[:6] in ('GIF87a', 'GIF89a'):
        # Check to see if content_type is correct
        content_type = 'image/gif'
        w, h = struct.unpack("<HH", data[6:10])
        width = int(w)
        height = int(h)

    # See PNG 2. Edition spec (http://www.w3.org/TR/PNG/)
    # Bytes 0-7 are below, 4-byte chunk length, then 'IHDR'
    # and finally the 4-byte width, height
    elif ((size >= 24) and data.startswith('\211PNG\r\n\032\n')
          and (data[12:16] == 'IHDR')):
        content_type = 'image/png'
        w, h = struct.unpack(">LL", data[16:24])
        width = int(w)
        height = int(h)

    # Maybe this is for an older PNG version.
    elif (size >= 16) and data.startswith('\211PNG\r\n\032\n'):
        # Check to see if we have the right content type
        content_type = 'image/png'
        w, h = struct.unpack(">LL", data[8:16])
        width = int(w)
        height = int(h)

    # handle JPEGs
    elif (size >= 2) and data.startswith('\377\330'):
        content_type = 'image/jpeg'
        jpeg = BytesIO(data_bytes)
        jpeg.read(2)
        b = jpeg.read(1)
        try:
            while (b and ord(b) != 0xDA):
                while (ord(b) != 0xFF): b = jpeg.read(1)
                while (ord(b) == 0xFF): b = jpeg.read(1)
                if (ord(b) >= 0xC0 and ord(b) <= 0xC3):
                    jpeg.read(3)
                    h, w = struct.unpack(">HH", jpeg.read(4))
                    break
                else:
                    jpeg.read(int(struct.unpack(">H", jpeg.read(2))[0])-2)
                b = jpeg.read(1)
            width = int(w)
            height = int(h)
        except struct.error:
            pass
        except ValueError:
            pass

    return content_type, width, height
