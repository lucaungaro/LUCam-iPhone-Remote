"""Camera fleet manager: one worker thread per camera, global commands,
discovery, persistence, and the JSON HTTP API the web UI talks to.

Cameras are listened to, not polled: see CameraWorker.

Nothing here blocks the caller: every network call runs on a worker thread,
so HTTP request handlers only ever read snapshots and queue commands.
"""

import collections
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

from . import api, discovery, events, filelog, livestream

PARAMS = ('lens', 'iso', 'fps', 'shutter', 'wb', 'tint', 'letter', 'reel', 'codec', 'resolution', 'colorspace',
		  'focus', 'autofocus', 'focusonce', 'focuslock', 'displaylut', 'cleanfeed', 'iris')

# Third-stop f-numbers offered for the iris, filtered to each camera's range.
F_STOPS = (1.0, 1.1, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.5, 2.8, 3.2, 3.5, 4.0, 4.5, 5.0, 5.6,
		   6.3, 7.1, 8.0, 9.0, 10.0, 11.0, 13.0, 14.0, 16.0, 18.0, 20.0, 22.0)


def lens_names(lenses):
	"""{lens id: name}. Back lenses go by focal length ('24 mm' is shown from
	mm, so they get it here too); front lenses get a name from their id
	('LensFront' -> 'Front', 'LensFrontUltraWide' -> 'Front ultra wide'), made
	unique when two would read the same."""
	names = {}
	for l in lenses:
		if l.get('facing') == 'front':
			words = re.findall(r'[A-Z][a-z]+|[A-Z]+(?![a-z])|\d+', re.sub(r'^Lens', '', l['id'])) or ['Front']
			name = ' '.join([words[0]] + [w.lower() for w in words[1:]])
			if not name.lower().startswith('front'):
				name = 'Front ' + name.lower()
		else:
			name = '{} mm'.format(l.get('focalLength'))
		if name in names.values():
			name = '{} ({})'.format(name, sum(1 for n in names.values() if n.startswith(name)) + 1)
		names[l['id']] = name
	return names


def nearest_lens(lenses, mm):
	"""The lens whose focal length is closest to mm (in stops, so 30 mm picks 24 over 48)."""
	by_mm = {l['focalLength']: l for l in lenses}
	return by_mm[nearest(mm, list(by_mm), log=True)]


def format_f_stop(value):
	"""1.78 -> 'f/1.78', 2.0 -> 'f/2', 2.8 -> 'f/2.8'"""
	return 'f/{}'.format(('%.2f' % float(value)).rstrip('0').rstrip('.'))


def parse_f_stop(value):
	"""'f/2.8', 'F2.8', '2.8' -> 2.8"""
	s = str(value).strip().lower().replace(' ', '')
	for prefix in ('f/', 't/', 'f', 't'):
		if s.startswith(prefix):
			s = s[len(prefix):]
			break
	return float(s)
# Per-display on/off switches: param -> (API feature, state key)
MONITOR_SWITCHES = {'displaylut': ('displayLUT', 'displayLuts'), 'cleanfeed': ('cleanFeed', 'cleanFeeds')}
FOCUS_PARAMS = ('focus', 'autofocus', 'focusonce')   # refused while a camera's focus is locked

# Recording format used while a camera is in live view: the app only streams
# when it records H.264, and only at whole-number frame rates.
LIVE_CODEC = 'H.264:High'
LIVE_HEIGHT = 1080
WHOLE_FPS = {'23.98': '24', '29.97': '30', '47.95': '48', '59.94': '60', '119.88': '60', '120': '60', '100': '50'}


def res_key(res):
	return '{}x{}'.format(res.get('width'), res.get('height')) if res else None


def codec_label(codec):
	"""'Apple ProRes 422 HQ:Default' -> 'ProRes 422 HQ', 'HEVC (H.265):High' -> 'HEVC High'"""
	name, _, quality = (codec or '').partition(':')
	name = name.replace('Apple ', '').replace(' (H.265)', '')
	return name if quality in ('', 'Default') else '{} {}'.format(name, quality)


def format_summary(fmt):
	if not fmt:
		return None
	res = fmt.get('recordResolution') or {}
	desc = (fmt.get('resolutionDescriptor') or {}).get('description') or res_key(res)
	return '{} · {} · {}'.format(codec_label(fmt.get('codec')), desc, fmt.get('frameRate'))


def bare_format(fmt):
	"""The /system/format object without our private keys (e.g. '_dynamicRange')."""
	return {k: v for k, v in fmt.items() if not k.startswith('_')}

# Native focal lengths (35 mm equivalent) per iPhone model, offered in the
# Lens dropdown: the physical lenses plus the main sensor's 2x crop where the
# Camera app treats it as a lens. Edit freely; any value can still be typed.
NATIVE_LENSES_MM = {
	'iPhone 12': (13, 26), 'iPhone 12 mini': (13, 26),
	'iPhone 12 Pro': (13, 26, 52), 'iPhone 12 Pro Max': (13, 26, 65),
	'iPhone 13': (13, 26), 'iPhone 13 mini': (13, 26),
	'iPhone 13 Pro': (13, 26, 77), 'iPhone 13 Pro Max': (13, 26, 77),
	'iPhone 14': (13, 26), 'iPhone 14 Plus': (13, 26),
	'iPhone 14 Pro': (13, 24, 48, 77), 'iPhone 14 Pro Max': (13, 24, 48, 77),
	'iPhone 15': (13, 26, 52), 'iPhone 15 Plus': (13, 26, 52),
	'iPhone 15 Pro': (13, 24, 48, 77), 'iPhone 15 Pro Max': (13, 24, 48, 120),
	'iPhone 16e': (26, 52),
	'iPhone 16': (13, 26, 52), 'iPhone 16 Plus': (13, 26, 52),
	'iPhone 16 Pro': (13, 24, 48, 120), 'iPhone 16 Pro Max': (13, 24, 48, 120),
	'iPhone 17': (13, 26, 52), 'iPhone Air': (26, 52),
	'iPhone 17 Pro': (13, 24, 48, 100, 200), 'iPhone 17 Pro Max': (13, 24, 48, 100, 200),
}

# Fallback when the model isn't in the table.
LENS_PRESETS_MM = (13, 24, 26, 28, 35, 48, 52, 77, 100, 120)

WEB_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'web'))


def parse_shutter(value, measurement):
	"""'1/50' -> speed 50, '180°' / '180deg' -> angle 180, bare numbers follow
	the camera's current measurement mode."""
	s = str(value).strip().lower().replace(' ', '')
	if not s:
		raise ValueError('Empty shutter value')
	if s.startswith('1/'):
		return {'speed': int(float(s[2:]))}
	m = re.match(r'^([\d.]+)(°|deg|d)$', s)
	if m:
		return {'angle': float(m.group(1))}
	n = float(s)
	if measurement == 'ShutterAngle':
		return {'angle': n}
	return {'speed': int(n)}


def nearest(value, options, log=False):
	"""The option closest to value; log=True compares ratios (stops), for ISO,
	shutter and focal lengths."""
	opts = [o for o in options if o is not None and (not log or o > 0)]
	if not opts:
		return value
	if log and value > 0:
		return min(opts, key=lambda o: abs(math.log(o / value)))
	return min(opts, key=lambda o: abs(o - value))


def clamp(value, rng):
	if rng and rng[0] is not None and rng[1] is not None:
		return max(rng[0], min(rng[1], value))
	return value


REEL_RANGE = (1, 999)


def format_shutter(info):
	if not info:
		return None
	if info.get('shutterAngle') is not None and info.get('shutterSpeed') is None:
		a = info['shutterAngle']
		return '{:g}°'.format(a / 100 if a > 360 else a)
	if info.get('shutterSpeed') is not None:
		return '1/{}'.format(info['shutterSpeed'])
	return None


class CameraWorker(threading.Thread):
	"""One camera: its event channel, its state, and every request sent to it.

	How it talks to a camera, and why. Blackmagic Camera for iOS 3.5 slows down
	over hours when its REST API is used while no client is connected to its
	event WebSocket, until recordings drop frames; only relaunching the app
	clears it. So:

	- The event channel is opened before anything is sent, and kept open. The
	  camera's state comes from its events. Nothing is read on a timer.
	- State is read once when the channel opens, then only on demand: after a
	  command, when a row is expanded, when a format change makes the lists of
	  supported values stale.
	- A camera without an event channel is polled, as before, and labelled as such.
	  So is one whose channel has dropped, until it is back; if that lasts, on a
	  camera that does have the channel, the worker raises the alarm.
	- With QUIET_DURING_TAKE, a recording camera is sent nothing but REC and STOP.
	- An unarmed camera is treated like any other: arming only decides whether it
	  takes global values and REC ALL.
	- One request at a time per camera (api.CameraClient holds the lock).
	- Every answer is timed. A camera whose answers slow down is flagged, since
	  the API reports neither dropped frames nor load.
	- The API reports no trouble by itself, so the worker raises the alarms: a
	  recording that stops unasked, a recording camera that goes away, a REC
	  or STOP the camera acknowledged but did not carry out.
	"""

	LOST_WHILE_RECORDING = 10  # s without the camera while it records before the alarm
	REC_GRACE = 3.0            # s after REC during which 'not recording' is not an alarm
	REC_CONFIRM = 3.0          # s to wait for the camera to confirm a REC or STOP by an event
	TICK = 1.0                 # housekeeping interval; a tick sends nothing by itself
	# Without the event channel the controller asks, as it always did: for a camera that has
	# none, and for one whose channel has dropped, until it is back.
	POLL_FAST = 0.5            # record state, timecode, focus
	POLL_FOCUS_LOCKED = 0.2    # while focus is locked: undo a tap to focus quickly
	POLL_FULL = 3.0            # all settings
	RETRY_OFFLINE = 2.0
	POLL_AFTER_DROP = 1.0      # s to let a dropped channel reopen before polling starts
	POLLED_ALARM_AFTER = 10.0  # s of polling a camera that has an event channel before the alarm
	FAST_PATHS = ('/transports/0/record', '/transports/0/timecode', '/lens/focus')
	PING_AFTER = 5.0           # s of silence on the event channel before a ping
	SILENT_AFTER = 10.0        # s of silence before the connection is taken for dead
	# The one read on a timer: a single small GET to time the app's answer, only for an
	# camera whose event channel is open (0.3 status line per request there,
	# against about 105 without a listener).
	HEALTH_INTERVAL = 30.0
	HEALTH_WINDOW = 5          # answers the median is taken over
	FRESH_MAX = 0.160          # s: a fresh app answers within this from a client on the same network
	WARN_DELAY = 0.100         # s above the camera's own baseline: warn
	CRITICAL_DELAY = 0.250     # s above it: relaunch the app before the next take
	BACK_OFF = 5.0             # s without requests of the worker's own after one went unanswered
	WB_RESTORE_EVERY = 3.0     # s between two restores of a held white balance
	FOCUS_RESTORE_EVERY = 0.3  # s between two restores of a locked focus
	LIVE_CHECK_EVERY = 10.0    # s between checks of a live stream that reports no video
	# True: a recording camera is sent nothing but REC and STOP by the worker itself (no timing
	# read, no restore of a held value until the take ends). Off: with the event channel open,
	# requests during a take did no harm in the long tests. On a phone that something has
	# already slowed, they did: the slow-app alarm is what tells you about such a phone.
	QUIET_DURING_TAKE = False

	# What is read when the event channel opens: (key marking it unsupported, path).
	CAPABILITY_PATHS = (
		('product', '/system/product'), ('iso', '/video/supportedISOs'), ('shutter', '/video/supportedShutters'),
		('wb', '/video/whiteBalance/description'), ('tint', '/video/whiteBalanceTint/description'),
		('fps', '/system/supportedFormats'), ('colorspace', '/system/supportedDynamicRanges'),
		('iris', '/lens/iris/description'), ('focus', '/lens/focus/description'),
		('lenscams', '/lens/cameras'), ('lens', '/lens/zoom/description'),
	)
	VALUE_PATHS = (
		('timecode', '/transports/0/timecode'), ('focus', '/lens/focus'), ('iso', '/video/iso'),
		('shutter', '/video/shutter'), ('wb', '/video/whiteBalance'), ('tint', '/video/whiteBalanceTint'),
		('fps', '/system/format'), ('colorspace', '/system/dynamicRange'), ('autofocus', '/lens/focus/autoFocus'),
		('iris', '/lens/iris'), ('lens', '/lens/zoom'), ('lenscams', '/lens/cameras/active'),
		('slate', '/slates/nextClip'), ('stream', '/livestreams/0'), ('stream', '/livestreams/0/activePlatform'),
	)
	PATH_KEYS = dict((p, k) for k, p in CAPABILITY_PATHS + VALUE_PATHS + (('record', '/transports/0/record'),))
	# Read back after a command (the event channel does not carry every value, and a
	# camera without one has no other way to confirm).
	READ_BACK = {
		'iso': ('/video/iso',), 'shutter': ('/video/shutter',), 'iris': ('/lens/iris',),
		'lens': ('/lens/zoom', '/lens/cameras/active'), 'letter': ('/slates/nextClip',), 'reel': ('/slates/nextClip',),
		'colorspace': ('/system/dynamicRange',), 'autofocus': ('/lens/focus/autoFocus',),
		'focusonce': ('/lens/focus/autoFocus',),
	}

	def __init__(self, manager, cam_id, address, name='', active=True, live_mode=False, saved_format=None,
				 focus_locked=False, wb_hold=None, focus_hold=None):
		super().__init__(name='lucam-' + cam_id, daemon=True)
		self.manager = manager
		# Live mode: the camera temporarily records H.264 1080p so it can stream;
		# `saved_format` is its real recording format, restored on exit and before REC.
		self.live_mode = bool(live_mode and saved_format)
		# Focus lock is ours (the app's own lock isn't in the API): AF off, focus
		# commands refused, and the position held: put back if the phone moves it
		# (tap to focus, an AF pass). Saved in cameras.json.
		self.focus_locked = bool(focus_locked)
		self.focus_hold = focus_hold if self.focus_locked else None   # None: take the next reading
		self._focus_restoring = False
		# White balance lock is ours too: the API has no auto-WB switch, so the
		# last WB/tint set from here is held and restored if the phone changes it.
		# Auto WB or applying a preset releases it. Saved in cameras.json.
		self.wb_hold = {k: v for k, v in (wb_hold or {}).items() if k in ('wb', 'tint') and v is not None}
		self._wb_restores = 0
		self._wb_last_restore = 0.0
		self._focus_target = None   # latest slider value; older ones are dropped
		self.saved_format = saved_format if self.live_mode else None
		self._format_lock = threading.Lock()
		self._supported = []
		self._lenses = []
		self._zoom_desc = {}
		self._front_lens = None     # name of the active lens when it is a front one
		self._platform = {}         # the camera's active streaming platform
		self._monitor_displays = {p: [] for p in MONITOR_SWITCHES}   # displays that have each switch
		self._iris_controllable = False
		self._iris_range = (None, None)
		self._device_format = None
		self._device_dr = None
		# Until one answers, cycle through the variants of what the user typed
		# (e.g. bare IP -> https://ip:4444, http://ip, https://ip).
		self.typed_address = address
		self.candidates = api.address_candidates(address)
		self.resolved = len(self.candidates) == 1
		self.client = self._make_client(self.candidates[0])
		self.commands = queue.Queue()
		self._halt = threading.Event()
		self.channel = None
		self._listening = False        # the event channel is open
		self._timecode_events = False  # this connection has delivered timecode events
		self._legacy = False           # this camera has no event channel
		self._connected = False        # its state has been read since it came online
		self._channel_down_since = None
		self._caps_dirty = True
		self._next_fast = 0.0          # polling, while there is no event channel
		self._next_full = 0.0
		self._had_channel = False      # its event channel has been open: it is not a camera without one
		self._lists_events = False     # it lists events to subscribe to, yet the channel cannot be opened
		self._polled_since = None
		self._offline_since = None
		self._resync_due = False       # values to re-read once that is allowed again
		self._auto_after = 0.0         # no request of the worker's own before this (monotonic)
		self._next_health = 0.0
		self._last_ping = 0.0
		self._timings = collections.deque(maxlen=self.HEALTH_WINDOW)
		self._baseline = None
		self._requests = 0
		self._hold_check = set()       # holds to compare with the camera at the next tick
		self._hold_after = {'wb': 0.0, 'focus': 0.0}
		self._reread_at = {}           # path -> when it was last re-read after an event we could not use
		self._was_recording = False
		self._rec_started = 0.0        # when the current recording began (ours or started on the phone)
		self._rec_expected = None      # True/False while a REC/STOP of ours waits for the camera's event
		self._rec_reported = None      # what the camera last said
		self._rec_confirm = threading.Event()
		self._expect_stop_until = 0.0
		self._lost_alarmed = False
		self.unsupported = set()
		self._shutter_options = {}
		self._angle_scale = 1
		self._zero_bitrate_since = None
		self._stream_format = None
		self._live_checked = 0.0
		self.state = {
			'id': cam_id,
			'name': name,
			'address': self.client.address,
			'active': bool(active),
			'online': False,
			'connecting': True,
			'error': None,
			'errorTime': 0,
			'product': '',
			'deviceName': '',
			'software': '',
			'recording': False,
			'recordPending': False,
			# set when a recording ended without LUCam asking for it (the camera stopped it:
			# dropped frames with "stop recording" on, full media, or a stop on the phone);
			# cleared by the next REC. {'t', 'after', 'timecode'}
			'stopAlert': None,
			# The event channel: 'connecting', 'listening', 'lost' (was open, being reopened),
			# 'none' (this camera does not open one: it is polled).
			'events': 'connecting', 'listenSince': 0, 'gaps': 0, 'gapSeconds': 0,
			# 'polled': the camera is being asked for its values on a timer, for want of an event
			# channel. 'pollAlert' ({'t'}): it has such a channel and has been polled for a while:
			# on Blackmagic Camera 3.5 that is what slows the app down.
			'polled': False, 'pollAlert': None,
			# How long the camera takes to answer a small read, median of the last few (ms),
			# against the fastest it has been since connecting. 'health': 'ok', 'warn', 'critical'.
			'answerMs': None, 'baselineMs': None, 'delayMs': None, 'health': None, 'answerTime': 0,
			'requests': 0,
			'timecode': None,
			'streaming': None, 'streamBitrate': 0, 'liveOurs': False, 'livePath': None, 'liveWarning': None,
			'lens': None, 'lensOptions': [], 'lensRange': None, 'lensNative': False, 'lensMain': None,
			'lensId': None,
			'iris': None, 'irisOptions': [], 'irisControllable': False, 'irisAuto': False,
			'focus': None, 'focusControllable': False, 'autofocus': None, 'autofocusSupported': False,
			'focusLocked': bool(focus_locked),
			'displayLuts': {},   # {'Device': False, 'HDMI': True}: display LUT on/off per display
			'cleanFeeds': {},    # {'HDMI': False}: clean feed (no overlays) per display
			'iso': None, 'isoOptions': [],
			'fps': None, 'fpsOptions': [],
			'codec': None, 'codecOptions': [], 'resolution': None, 'resolutionOptions': [],
			'colorspace': None, 'colorspaceOptions': [],
			'liveMode': self.live_mode, 'deviceFormat': None,
			'shutter': None, 'shutterMode': None, 'shutterOptions': [],
			'wb': None, 'wbRange': None, 'wbHold': dict(self.wb_hold),
			'tint': None, 'tintRange': None,
			'letter': None, 'reel': None, 'clipName': None,
			'unsupported': [],
			'lastSeen': 0,
		}

	# --- thread-safe accessors ---------------------------------------------

	def snapshot(self):
		with self.manager.lock:
			s = dict(self.state)
		s['unsupported'] = sorted(self.unsupported)
		return s

	def _update(self, **kw):
		with self.manager.lock:
			self.state.update(kw)

	def _label(self):
		return self.state['name'] or self.state['address']

	def _error(self, msg):
		self._update(error=msg, errorTime=time.time())
		self.manager._log('{}: {}'.format(self._label(), msg))

	def _camera_stopped(self, tc):
		"""The camera reports it is not recording when LUCam believed it was, and no STOP
		was sent: the camera (or someone at the phone) ended the clip. With the app's
		dropped-frames option on "stop recording" this is the live dropped-frame alarm."""
		now = time.time()
		after = now - self._rec_started if self._rec_started else None
		alert = {'t': now, 'after': after, 'timecode': tc, 'kind': 'stopped'}
		self._update(stopAlert=alert)
		msg = 'RECORDING STOPPED BY THE CAMERA{}{} - LUCam sent no STOP (dropped frames with ' \
			  '"stop recording" on, media full, or a stop on the phone)'.format(
				  ' after {:.0f}s'.format(after) if after is not None else '',
				  ' at tc {}'.format(tc) if tc else '')
		self._error(msg)
		self.manager.alert('{}: {}'.format(self._label(), msg))

	def stop(self):
		self._halt.set()
		self.commands.put(None)
		if self.channel:
			self.channel.stop()

	# --- main loop ------------------------------------------------------------

	def run(self):
		self._start_channel()
		while not self._halt.is_set():
			wait = self.TICK
			if self._hold_check:   # a restore is owed: wake when it may be sent
				due = min(self._hold_after[k] for k in self._hold_check) - time.monotonic()
				wait = max(0.02, min(wait, due))
			if self.state['polled'] or self._legacy:
				wait = max(0.02, min(wait, self._next_fast - time.monotonic()))
			try:
				cmd = self.commands.get(timeout=wait)
			except queue.Empty:
				cmd = False
			if self._halt.is_set():
				break
			if cmd:
				self._execute(*cmd)
			try:
				self._tick()
			except api.ApiError as e:
				self._unanswered('housekeeping', e)

	def _make_client(self, address):
		client = api.CameraClient(address)
		client.on_request = self._timing
		return client

	def _start_channel(self):
		"""Open the event channel. Nothing is sent to the camera before it is open."""
		addresses = [self.state['address']] if self.resolved else self.candidates
		self.channel = events.EventChannel(
			addresses, on_event=self._on_event, on_open=self._on_open, on_close=self._on_close,
			on_refused=lambda address: self.commands.put(('refused', address)),
			name='lucam-events-' + self.state['id'])
		self.channel.start()

	# Called on the event channel's thread.

	def _on_open(self, address):
		down, self._channel_down_since = self._channel_down_since, None
		self._timecode_events = False
		self._listening = True
		self.commands.put(('open', address, None if down is None else time.monotonic() - down))

	def _on_close(self, reason):
		self._listening = False
		self._channel_down_since = time.monotonic()
		self.commands.put(('closed', reason))

	def _on_event(self, prop, value):
		# Tally and timecode are applied at once; the rest goes through the worker's
		# queue, in order with its own requests.
		if isinstance(value, dict):
			if prop == '/transports/0/timecode' and value.get('display') is not None:
				self._timecode_events = True
				self._update(timecode=value['display'], lastSeen=time.time())
				return
			if prop == '/transports/0/record' and 'recording' in value:
				self._on_record(bool(value['recording']))
				return
		self.commands.put(('event', prop, value))

	# --- connection -------------------------------------------------------------

	def _opened(self, address, gap=None):
		"""The event channel is open: read the camera's state under it."""
		if address != self.client.address or not self.resolved:
			self.client = self._make_client(address)
			self.resolved = True
			self._update(address=self.client.address)
			self.manager._save_later()
		self._legacy = False
		self._had_channel = True
		self._lost_alarmed = False
		self._offline_since = None
		upd = {'events': 'listening', 'listenSince': time.time()}
		if gap is not None:
			upd.update(gaps=self.state['gaps'] + 1, gapSeconds=round(self.state['gapSeconds'] + gap, 1))
			self.manager._log('{}: event channel back after {:.1f} s'.format(self._label(), gap))
		first = not self._connected
		self._update(online=True, connecting=False, lastSeen=time.time(), **upd)
		self._auto_after = 0.0
		self._resync_due = True
		if first:
			self.manager._log('{}: event channel open'.format(self._label()))
		if not self._listening:
			return   # it dropped again already: the next opening does the reading
		try:
			self._read('record', '/transports/0/record')
			if self.state['recording'] and self.QUIET_DURING_TAKE:
				if first:
					self.manager._log('{}: it is recording; its settings are read when the take ends'.format(self._label()))
				return
			self._resync(first=first)
		except api.ApiError as e:
			self._unanswered('reading the camera', e)   # tried again from the tick

	def _closed(self, reason):
		if self._connected and not self._legacy:
			self._update(events='lost')
			self.manager._log('{}: event channel lost ({}); reconnecting'.format(self._label(), reason))
		self._next_fast = self._next_full = 0.0

	def _refused(self, address):
		"""A server answers at this address without an event channel (another Blackmagic
		camera, an older app): it is polled, as the controller always did."""
		if self._legacy:
			return
		if api.probe(address, timeout=1.5) is None:
			return
		if address != self.client.address or not self.resolved:
			self.client = self._make_client(address)
			self.resolved = True
			self._update(address=self.client.address)
			self.manager._save_later()
		self._legacy = True
		self._next_fast = self._next_full = 0.0
		self._update(events='none')
		try:
			listing = self._try('eventlist', self.client.get, '/event/list')
		except api.ApiError:
			listing = None
		props = listing.get('events') if isinstance(listing, dict) else listing
		self._lists_events = bool(props)   # it says it has events, and the channel will not open
		self.manager._log('{}: no event channel on this camera: it is polled'.format(self._label()))

	def _went_offline(self, reason):
		if self.state['online'] or self.state['connecting']:
			self._error('offline ({})'.format(reason))
			self._offline_since = self._channel_down_since or time.monotonic()
		self._update(online=False, connecting=False)
		self._connected = False
		self._caps_dirty = True

	def _poll(self, now):
		"""Ask the camera for its values: record state, timecode and focus twice a second,
		the settings every 3 s. The next pass is timed from the end of this one, so a
		camera that answers slowly is asked less often, never without a pause."""
		s = self.state
		if s['recordPending'] or now < self._auto_after or now < self._next_fast:
			return
		try:
			if not self._connected or self._caps_dirty:
				self._read_all()
				self._next_full = time.monotonic() + self.POLL_FULL
			else:
				for path in self.FAST_PATHS:
					self._read(self.PATH_KEYS[path], path)
				if now >= self._next_full:
					self._read_values(skip=self.FAST_PATHS)
					self._next_full = time.monotonic() + self.POLL_FULL
		except api.ApiError as e:
			self._went_offline(e)
			self._next_fast = self._next_full = time.monotonic() + self.RETRY_OFFLINE
			return
		self._offline_since = None
		self._lost_alarmed = False
		self._update(online=True, connecting=False, lastSeen=time.time())
		self._next_fast = time.monotonic() + (self.POLL_FOCUS_LOCKED if self.focus_locked else self.POLL_FAST)

	def _watch_polling(self, polled):
		"""Say that a camera is polled, and raise the alarm when one that has an event
		channel stays polled: nothing is listening to it, which is what slows the app."""
		s = self.state
		polled = bool(polled and s['online'])
		if polled != s['polled']:
			self._update(polled=polled)
		if polled and (self._had_channel or self._lists_events):
			now = time.monotonic()
			if self._polled_since is None:
				self._polled_since = now
			elif not s['pollAlert'] and now - self._polled_since > self.POLLED_ALARM_AFTER:
				self._update(pollAlert={'t': time.time()})
				self.manager.alert('{}: POLLED WITH NOTHING LISTENING - its event channel cannot be opened, so the '
								   'controller asks it for its values about 11 times a second. On Blackmagic Camera '
								   '3.5 that slows the app down over hours, until recordings drop frames. Press the '
								   'reconnect button; if it stays polled, stop the controller'.format(self._label()))
		else:
			self._polled_since = None
			if s['pollAlert']:
				self._update(pollAlert=None)

	def _resync(self, first=False, light=False):
		"""Read the camera's state once. Deferred while that is not allowed (it records
		and takes are kept quiet)."""
		if self._legacy:
			self._read_all() if not light else self._read_unbroadcast()
			return
		if not self._listening:
			if not light:
				self._resync_due = True
			return
		if not self._may_send():
			self._resync_due = self._resync_due or not light
			return
		if light:
			self._read_unbroadcast()
			return
		self._resync_due = False
		try:
			if first or self._caps_dirty or not self._connected:
				self._read_all()
				self._subscribe_all()
				if first:
					self.manager._log('{}: connected, listening to its events'.format(self._label()))
			else:
				self._read_values()
		except api.ApiError:
			self._resync_due = True
			raise

	def _read_all(self):
		self._load_capabilities()
		self._read_values(monitors=False)
		self._connected = True

	def _subscribe_all(self):
		"""Subscribe to everything the camera can report, as in the long tests."""
		listing = self._try('eventlist', self.client.get, '/event/list')
		props = listing.get('events') if isinstance(listing, dict) else listing
		if isinstance(props, list):
			self.channel.subscribe([p for p in props if isinstance(p, str)])

	def _may_send(self):
		"""Whether the worker may send this camera a request of its own accord."""
		s = self.state
		return bool(s['online'] and not s['recordPending']
					and not (self.QUIET_DURING_TAKE and s['recording'] and self._listening)
					and time.monotonic() >= self._auto_after)

	def _unanswered(self, what, e):
		"""A request of the worker's own went unanswered: say so and hold off."""
		self._auto_after = time.monotonic() + self.BACK_OFF
		self._error('{}: no answer from the camera ({})'.format(what, e))

	def _try(self, key, fn, *args):
		"""Run one GET; returns None when the endpoint is unsupported.
		Raises ApiError only for connection failures."""
		if key in self.unsupported:
			return None
		try:
			return fn(*args)
		except api.ApiError as e:
			if e.status == 0:
				raise
			if e.status in (404, 405, 501):
				self.unsupported.add(key)
			return None

	def _read(self, key, path):
		"""GET one path and fold the answer into the state."""
		value = self._try(key, self.client.get, path)
		if value is not None:
			self._apply(path, value)
		return value

	# --- housekeeping -------------------------------------------------------------

	def _tick(self):
		now = time.monotonic()
		s = self.state
		down = self._channel_down_since
		# No event channel (the camera has none, or it dropped a moment ago): ask instead.
		# A dropped channel reopens by itself; once the camera stops answering, its return
		# is left to the channel, so that the listener is there first again.
		polled = self._legacy or (down is not None and now - down >= self.POLL_AFTER_DROP and s['online'])
		if polled:
			self._poll(now)
		self._watch_polling(polled)
		if not s['online']:
			since = self._offline_since
			if s['recording'] and not s['stopAlert'] and not self._lost_alarmed and since is not None \
					and now - since > self.LOST_WHILE_RECORDING:
				# a recording camera that stays unreachable: the app died or was left
				self._lost_alarmed = True
				self._update(stopAlert={'t': time.time(), 'after': None, 'timecode': s.get('timecode'), 'kind': 'lost'})
				self.manager.alert('{}: RECORDING CAMERA LOST for {:.0f}s (app not answering: crashed, closed or '
								   'phone locked?) last tc {}'.format(self._label(), now - since, s.get('timecode')))
			return
		if self._listening:
			quiet = now - self.channel.last_rx
			if quiet > self.PING_AFTER and now - self._last_ping > self.PING_AFTER:
				self._last_ping = now
				self.channel.ping()
			# Silence where there must be traffic: pings it used to answer, or the
			# timecode of a recording. Reopen (the camera is reported lost if that fails).
			if quiet > self.SILENT_AFTER and (self.channel.pongs or s['recording'] and self._timecode_events):
				self.channel.reset()
				return
		# what the camera last said about recording, once the grace after REC is over
		if s['recording'] and self._rec_reported is False and self._rec_expected is None \
				and time.time() - self._rec_started >= self.REC_GRACE:
			self._on_record(False)
		# transitions that make deferred work due
		if self._was_recording and not s['recording']:
			self._hold_check.update(k for k in ('wb', 'focus') if self._holding(k))
			self._next_health = min(self._next_health, now + 2)
		self._was_recording = s['recording']
		if self._resync_due and self._listening and self._may_send():
			self._resync(first=not self._connected)
		elif self._caps_dirty and self._connected and self._may_send():
			self._load_capabilities()
		self._enforce_holds(now)
		if now >= self._next_health and self._listening and self._may_send():
			self._next_health = now + self.HEALTH_INTERVAL
			try:
				self._read('record', '/transports/0/record')
			except api.ApiError as e:
				self._unanswered('health check', e)
				if now - self.channel.last_rx > self.SILENT_AFTER:
					self.channel.reset()   # neither events nor answers: reopen
		self._check_live(now)

	def _timing(self, method, path, status, took):
		"""Called after every request to this camera. Small reads are comparable with
		each other: their median, against the fastest it has been, is the app's delay."""
		self._requests += 1
		if method != 'GET' or path.startswith(('/presets', '/livestreams/platforms', '/event')):
			self._update(requests=self._requests)
			return
		if status == 0 and took < 0.9 * self.client.timeout:
			self._update(requests=self._requests)
			return   # refused or reset at once: says nothing about the app's delay
		self._timings.append(took)
		upd = {'requests': self._requests}
		if len(self._timings) >= 3:
			median = sorted(self._timings)[len(self._timings) // 2]
			self._baseline = min(median, self._baseline if self._baseline is not None else median, self.FRESH_MAX)
			delay = median - self._baseline
			health = 'critical' if delay >= self.CRITICAL_DELAY else 'warn' if delay >= self.WARN_DELAY else 'ok'
			was = self.state['health']
			upd.update(answerMs=round(median * 1000), baselineMs=round(self._baseline * 1000),
					   delayMs=round(delay * 1000), health=health, answerTime=time.time())
			self._update(**upd)
			if health != was and health == 'critical':
				self.manager.alert('{}: APP SLOW - answers take {:.0f} ms, {:.0f} ms more than a fresh app. Recordings '
								   'may drop frames: quit and relaunch Blackmagic Camera on this phone before the '
								   'next take'.format(self._label(), median * 1000, delay * 1000))
			elif health != was and health == 'warn':
				self.manager._log('{}: answers are slowing ({:.0f} ms, {:.0f} ms more than a fresh app)'.format(
					self._label(), median * 1000, delay * 1000))
			elif health == 'ok' and was in ('warn', 'critical'):
				self.manager._log('{}: answers back to normal ({:.0f} ms)'.format(self._label(), median * 1000))
			return
		self._update(**upd)

	# --- state: one property at a time, from a read or from an event ----------------

	def _event(self, prop, value):
		"""An event from the queue."""
		m = re.match(r'^/monitoring/([^/]+)/(displayLUT|cleanFeed)$', prop)
		if prop not in self.PATH_KEYS and not m:
			return   # subscribed to, not used
		try:
			if not isinstance(value, dict):
				raise TypeError('not an object')
			self._apply(prop, value)
		except (KeyError, TypeError, ValueError, AttributeError):
			# Not the object a GET returns: read the value instead, at most every 5 s.
			now = time.monotonic()
			if prop in self.PATH_KEYS and now - self._reread_at.get(prop, 0) > 5 and self._may_send():
				self._reread_at[prop] = now
				self._read(self.PATH_KEYS[prop], prop)

	def _apply(self, path, v):
		"""Fold one property's value into the state. `v` is what a GET of `path`
		returns, which is also what its event carries."""
		upd = {}
		if path == '/transports/0/record':
			self._on_record(bool(v.get('recording')))
			return
		elif path == '/transports/0/timecode':
			if v.get('display') is not None:
				upd['timecode'] = v['display']
		elif path == '/system/product':
			upd = {'product': v.get('productName', ''), 'deviceName': v.get('deviceName', ''),
				   'software': v.get('softwareVersion', '')}
			if not self.state['name'] and upd['deviceName']:
				upd['name'] = upd['deviceName']
				self.manager._save_later()
		elif path == '/video/supportedISOs':
			if v.get('supportedISOs'):
				upd['isoOptions'] = v['supportedISOs']
		elif path == '/video/supportedShutters':
			self._shutter_options = v
			upd['shutterOptions'] = self._shutter_option_labels(self.state['shutterMode'])
		elif path in ('/video/whiteBalance/description', '/video/whiteBalanceTint/description'):
			key = 'wb' if path == '/video/whiteBalance/description' else 'tint'
			d = v.get('whiteBalance' if key == 'wb' else 'whiteBalanceTint') or {}
			upd[key + 'Range'] = [d.get('min'), d.get('max')] if d else None
		elif path == '/system/supportedFormats':
			self._supported = v.get('supportedFormats', [])
			upd['resolutionOptions'] = [{
				'value': res_key(f.get('recordResolution')),
				'label': '{} · {}×{}'.format((f.get('resolutionDescriptor') or {}).get('description') or '',
											 f['recordResolution']['width'], f['recordResolution']['height']).strip(' ·'),
			} for f in self._supported if f.get('recordResolution')]
			upd.update(self._format_state())
		elif path == '/system/supportedDynamicRanges':
			upd['colorspaceOptions'] = v.get('supportedDynamicRanges', [])
		elif path == '/lens/iris/description':
			self._iris_controllable = bool(v.get('controllable'))
			rng = v.get('apertureStop') or {}
			lo, hi = rng.get('min'), rng.get('max')
			self._iris_range = (lo, hi)
			if self._iris_controllable and lo is not None and hi is not None:
				stops = sorted({round(lo, 2), round(hi, 2)} | {f for f in F_STOPS if lo - 0.01 <= f <= hi + 0.01})
				upd['irisOptions'] = [format_f_stop(f) for f in stops]
			else:
				upd['irisOptions'] = []
			upd['irisControllable'] = self._iris_controllable
		elif path == '/lens/focus/description':
			upd['focusControllable'] = bool(v.get('controllable'))
		elif path == '/lens/cameras':
			self._lenses = [l for l in (v.get('cameras') or []) if l.get('isAvailable', True) and l.get('id')]
			upd.update(self._lens_options())
		elif path == '/lens/zoom/description':
			self._zoom_desc = v
			upd.update(self._lens_options())
		elif path == '/lens/focus':
			focus = v.get('normalised', v.get('normalized'))
			if focus is not None:
				upd['focus'] = focus
				if self.focus_locked:
					if self.focus_hold is None:
						self.focus_hold = focus
						self.manager._save_later()
					self._hold_check.add('focus')
		elif path == '/video/iso':
			if v.get('iso') is not None:
				upd['iso'] = v['iso']
		elif path == '/video/shutter':
			upd['shutter'] = format_shutter(v)
			mode = 'ShutterAngle' if v.get('shutterSpeed') is None and v.get('shutterAngle') is not None else 'ShutterSpeed'
			upd['shutterMode'] = mode
			if mode == 'ShutterAngle':
				self._angle_scale = 100 if v['shutterAngle'] > 360 else 1
			upd['shutterOptions'] = self._shutter_option_labels(mode)
		elif path in ('/video/whiteBalance', '/video/whiteBalanceTint'):
			key, field = ('wb', 'whiteBalance') if path == '/video/whiteBalance' else ('tint', 'whiteBalanceTint')
			if v.get(field) is not None:
				upd[key] = v[field]
				if key in self.wb_hold:
					self._hold_check.add('wb')
		elif path == '/system/format':
			if v:
				old = self._device_format
				if old and (v.get('frameRate') != old.get('frameRate')            # shutter/ISO lists can depend on frame rate
							or v.get('recordResolution') != old.get('recordResolution')):   # turning the phone swaps its format list
					self._caps_dirty = True
				self._device_format = v
				upd.update(self._format_state())
		elif path == '/system/dynamicRange':
			if v.get('dynamicRange') is not None:
				self._device_dr = v['dynamicRange']
				upd.update(self._format_state())
		elif path == '/lens/focus/autoFocus':
			if v:
				upd['autofocusSupported'] = bool(v.get('supported', True))
				upd['autofocus'] = 'Continuous' if v.get('enabled') and v.get('mode') == 'Continuous' else 'Off'
		elif path == '/lens/iris':
			if v.get('apertureStop') is not None:
				upd['iris'] = format_f_stop(v['apertureStop'])
				upd['irisAuto'] = bool(v.get('continuousApertureAutoExposure'))
		elif path == '/lens/zoom':
			if v:
				upd['lens'] = self._front_lens or v.get('focalLength')
		elif path == '/lens/cameras/active':
			if v:
				upd['lensId'] = v.get('id')
				if v.get('facing') == 'front':
					self._front_lens = lens_names(self._lenses).get(v.get('id'), 'Front')
					upd['lens'] = self._front_lens
				elif self._front_lens:
					self._front_lens = None
					if v.get('focalLength') is not None:
						upd['lens'] = v['focalLength']
		elif path == '/slates/nextClip':
			if v:
				upd['letter'] = (v.get('project') or {}).get('camera')
				upd['reel'] = (v.get('clip') or {}).get('reel')
				upd['clipName'] = (v.get('clip') or {}).get('clipName')
		elif path == '/livestreams/0':
			if v:
				status, bitrate = v.get('status'), v.get('bitrate') or 0
				upd['streaming'] = status
				upd['streamBitrate'] = bitrate
				if status in (None, 'Idle'):
					upd['liveOurs'] = False
				elif self._platform:
					upd['liveOurs'] = self._platform.get('platform') == livestream.PLATFORM_ID
				self._stream_format = v.get('effectiveVideoFormat')
				upd['liveWarning'] = self._stream_warning(status, bitrate, self._stream_format)
		elif path == '/livestreams/0/activePlatform':
			self._platform = v
			if self.state['streaming'] not in (None, 'Idle'):
				upd['liveOurs'] = v.get('platform') == livestream.PLATFORM_ID
		else:
			m = re.match(r'^/monitoring/([^/]+)/(displayLUT|cleanFeed)$', path)
			if m and 'enabled' in v:
				display = urllib.parse.unquote(m.group(1))
				for param, (feature, key) in MONITOR_SWITCHES.items():
					if feature != m.group(2):
						continue
					known = self._monitor_displays[param]
					if display == 'LCD' and 'LCD' not in known and 'Device' in known:
						display = 'Device'   # the event list names the phone's screen LCD, the REST paths Device
					if display in known:
						upd[key] = dict(self.state.get(key) or {}, **{display: bool(v['enabled'])})
		if upd:
			self._update(lastSeen=time.time(), **upd)

	def _on_record(self, rec):
		"""The camera says it is, or is not, recording (an event, or a read)."""
		stopped = False
		with self.manager.lock:
			s = self.state
			prev, now = s['recording'], time.time()
			self._rec_reported = rec
			if self._rec_expected is not None and rec == self._rec_expected:
				self._rec_confirm.set()
			if rec and not prev:
				if self._rec_expected is not True:
					self._rec_started = now   # started on the phone
				s['recording'] = True
			elif prev and not rec:
				if self._rec_expected is False or time.monotonic() < self._expect_stop_until:
					s['recording'] = False    # the STOP we sent
				elif now - self._rec_started < self.REC_GRACE:
					pass   # the camera is still opening its file (1.2 s on an SSD); looked at again after the grace
				else:
					s['recording'] = False
					stopped = True
			s['lastSeen'] = now
			tc = s.get('timecode')
		if stopped:
			self._camera_stopped(tc)

	def _shutter_option_labels(self, mode):
		opts = self._shutter_options or {}
		if mode == 'ShutterAngle':
			return [format_shutter({'shutterAngle': a}) for a in opts.get('shutterAngles', [])]
		return ['1/{}'.format(s) for s in opts.get('shutterSpeeds', [])]

	def _lens_options(self):
		"""The Lens list: the phone's own lenses, else focal lengths within its zoom range."""
		upd = {}
		fl = (self._zoom_desc or {}).get('focalLength') or {}
		if self._lenses:
			# The phone's own lens list: switching selects the physical lens.
			back = sorted((l for l in self._lenses if l.get('facing') != 'front'), key=lambda l: l.get('focalLength', 0))
			front = [l for l in self._lenses if l.get('facing') == 'front']
			names = lens_names(back + front)
			upd['lensOptions'] = [{'value': l['id'], 'mm': l.get('focalLength'), 'zoom': (l.get('zoomFactor') or '').replace('x', '×'),
								   'facing': l.get('facing', 'back'), 'label': names[l['id']]} for l in back + front]
			upd['lensNative'] = True
			upd['lensMain'] = next((l.get('focalLength') for l in back if l.get('zoomFactor') == '1x'), None)
			if fl.get('min') is not None:
				upd['lensRange'] = [fl['min'], fl.get('max')]
		elif fl.get('min') is not None and fl.get('max') is not None:
			lo, hi = fl['min'], fl['max']
			upd['lensRange'] = [lo, hi]
			native = NATIVE_LENSES_MM.get((self.state['product'] or '').strip())
			if native:
				opts = [mm for mm in native if lo <= mm <= hi]
			else:
				opts = sorted({lo, hi} | {mm for mm in LENS_PRESETS_MM if lo <= mm <= hi})
			upd['lensOptions'] = opts
			upd['lensNative'] = bool(native)
			# 1x reference for the zoom factor shown next to each focal length
			upd['lensMain'] = min(opts, key=lambda mm: abs(mm - 25)) if opts else None
		return upd

	def _load_capabilities(self):
		"""What the camera can do: lists of supported values, ranges, lenses, displays."""
		self._caps_dirty = False
		try:
			for key, path in self.CAPABILITY_PATHS:
				self._read(key, path)
			self._read_monitors(discover=True)
		except api.ApiError:
			self._caps_dirty = True
			raise

	def _read_monitors(self, discover=False):
		"""On/off switches per display. With `discover`, find which displays have each:
		the listed ones plus HDMI (not listed). The phone screen has a LUT switch but no
		clean feed; HDMI has both."""
		c = self.client
		names = list(dict.fromkeys((self._try('displaylut', c.displays) or []) + ['HDMI'])) if discover else None
		upd = {}
		for param, (feature, key) in MONITOR_SWITCHES.items():
			values = {}
			for name in (names if discover else self._monitor_displays[param]):
				try:
					values[name] = c.monitor_switch(name, feature)
				except api.ApiError as e:
					if e.status == 0:
						raise
			if discover:
				self._monitor_displays[param] = list(values)
			upd[key] = values
		self._update(**upd)

	def _read_values(self, monitors=True, skip=()):
		"""Every setting, once."""
		for key, path in self.VALUE_PATHS:
			if path in skip or path == '/lens/cameras/active' and not self._lenses:
				continue
			self._read(key, path)
		if monitors:
			self._read_monitors()

	def _read_unbroadcast(self):
		"""The values the event channel does not carry (read when a row is expanded)."""
		self._read('autofocus', '/lens/focus/autoFocus')
		self._read_monitors()

	def _check_live(self, now):
		"""A live stream that reports no video: confirm by a read before warning, then
		look again now and then (the event channel may not carry every bitrate change)."""
		s = self.state
		if not (s['liveOurs'] and s['streaming'] == 'Streaming' and not s['streamBitrate']):
			return
		if self._zero_bitrate_since is None or now - self._zero_bitrate_since < 8:
			return
		if now - self._live_checked >= self.LIVE_CHECK_EVERY and self._may_send():
			self._live_checked = now
			self._read('stream', '/livestreams/0')

	def _start_live(self):
		"""Point the camera's live stream at our MediaMTX and start it."""
		c = self.client
		try:
			self.manager.live.ensure_running()
		except RuntimeError as e:
			raise api.ApiError(0, str(e))
		rtmp_url = 'rtmp://{}:{}/live'.format(self.manager.live_address_for(c.host), livestream.RTMP_PORT)
		platforms = c.get('/livestreams/platforms') or []
		for legacy in livestream.LEGACY_PLATFORMS:
			if legacy in platforms:
				try:  # the phone deletes custom platforms by name, not by file name
					c.request('DELETE', '/livestreams/customPlatforms/' + urllib.parse.quote(legacy))
				except api.ApiError:
					pass
		current = c.platform(livestream.PLATFORM_ID) if livestream.PLATFORM_ID in platforms else {}
		configs = sum(len(p.get('configs', [])) for p in current.get('profiles', []))
		urls = [s.get('url') for s in current.get('servers', [])]
		if rtmp_url not in urls or configs != len(livestream.STREAM_RATES) * len(livestream.STREAM_BITRATES):
			if current:
				# The phone won't overwrite an existing platform ("Platforms import
				# failed"), so a changed Mac address needs delete-then-upload.
				if (c.stream_status() or {}).get('status') not in (None, 'Idle'):
					c.stream(False)
				c.request('DELETE', '/livestreams/customPlatforms/' + urllib.parse.quote(livestream.PLATFORM_ID))
			c.upload_platform(livestream.PLATFORM_FILE, livestream.platform_xml(rtmp_url))
			urls = [s.get('url') for s in c.platform(livestream.PLATFORM_ID).get('servers', [])]
			if rtmp_url not in urls:
				raise api.ApiError(0, 'the camera did not accept the live platform for {}'.format(rtmp_url))
			self.manager._log('{}: live platform set to {}'.format(self._label(), rtmp_url))
		status = (c.stream_status() or {}).get('status')
		key = self.state['id']
		plat = c.active_platform()
		ours = plat.get('platform') == livestream.PLATFORM_ID and plat.get('key') == key
		if status not in (None, 'Idle') and not ours:
			c.stream(False)  # streaming elsewhere (or under another key): switch it over
			time.sleep(1)
			status = 'Idle'
		if not ours:
			c.set_active_platform(livestream.PLATFORM_ID, 'Mac', key, livestream.PROFILE)
		if status in (None, 'Idle'):
			c.stream(True)
		self._zero_bitrate_since = None
		self._platform = {'platform': livestream.PLATFORM_ID, 'key': key}
		self._update(livePath='live/' + key, liveOurs=True, streaming='Connecting')
		self.manager._log('{}: live view started'.format(self._label()))

	def _reconnect(self):
		"""Drop everything learned about the camera and connect from scratch:
		new event channel, re-resolve the address, re-read everything."""
		if self.channel:
			self.channel.stop()
		self.unsupported.clear()
		self._shutter_options = {}
		self._angle_scale = 1
		self.resolved = len(self.candidates) == 1
		self.client = self._make_client(self.candidates[0] if not self.resolved else self.state['address'])
		self._listening = self._legacy = self._connected = False
		self._had_channel = self._lists_events = False
		self._polled_since = self._offline_since = None
		self._next_fast = self._next_full = 0.0
		self._channel_down_since = None
		self._caps_dirty = True
		self._resync_due = False
		self._auto_after = 0.0
		self._timings.clear()
		self._baseline = None
		self._update(connecting=True, online=False, error=None, errorTime=0, recordPending=False, events='connecting',
					 answerMs=None, baselineMs=None, delayMs=None, health=None, polled=False, pollAlert=None)
		self.manager._log('{}: reconnecting'.format(self._label()))
		self._start_channel()

	# --- recording format -------------------------------------------------------

	def _format_state(self):
		"""UI fields for the real recording format (the saved one while live)."""
		shown = self.saved_format if self.live_mode else self._device_format
		if not shown:
			return {}
		entry = self._entry_for(shown)
		return {
			'fps': shown.get('frameRate'),
			'colorspace': shown.get('_dynamicRange') if self.live_mode else self._device_dr,
			'codec': shown.get('codec'),
			# a vertical crop shows as the format it's cut from (e.g. 1214x2160 -> 4K)
			'resolution': res_key((entry or shown).get('recordResolution')),
			'codecOptions': [{'value': c, 'label': codec_label(c)} for c in (entry or {}).get('codecs', [])],
			'fpsOptions': (entry or {}).get('frameRates', []),
			'liveMode': self.live_mode,
			'deviceFormat': format_summary(self._device_format),
		}

	def _entry(self, resolution):
		for f in self._supported:
			if res_key(f.get('recordResolution')) == resolution:
				return f
		return None

	def _entry_for(self, fmt):
		"""Supported-format entry for a format. In vertical mode the phone records
		a crop (e.g. 1214x2160) that isn't listed: match it to its 16:9 source by
		descriptor ('4K', 'HD'...) and height."""
		res = fmt.get('recordResolution') or {}
		entry = self._entry(res_key(res))
		if entry is None:
			desc = (fmt.get('resolutionDescriptor') or {}).get('description')
			entry = next((f for f in self._supported
						  if desc and (f.get('resolutionDescriptor') or {}).get('description') == desc), None) or \
				next((f for f in self._supported if (f.get('recordResolution') or {}).get('height') == res.get('height')), None)
		return entry

	def _build_format(self, base, codec=None, resolution=None, fps=None):
		"""Full /system/format object (the phone rejects partial ones)."""
		fmt = dict(base)
		entry = self._entry(resolution) if resolution else self._entry_for(base)
		if entry is None:
			raise ValueError('unsupported resolution {}'.format(resolution))
		if resolution:
			for k in ('recordResolution', 'sensorResolution', 'resolutionDescriptor'):
				if k in entry:
					fmt[k] = entry[k]
		if codec:
			fmt['codec'] = codec
		if fps:
			fmt['frameRate'] = str(fps)
		if entry.get('codecs') and fmt.get('codec') not in entry['codecs']:
			raise ValueError('{} is not available at {}'.format(codec_label(fmt.get('codec')), res_key(fmt['recordResolution'])))
		if entry.get('frameRates') and fmt.get('frameRate') not in entry['frameRates']:
			raise ValueError('{} fps is not available at {}'.format(fmt.get('frameRate'), res_key(fmt['recordResolution'])))
		return fmt

	def _apply_format(self, fmt, timeout=8.0, need_resolution=True):
		"""PUT a full format and wait until the phone reports it. With
		need_resolution=False, a resolution the phone kept is accepted: in
		vertical mode it takes codec changes but ignores resolution ones."""
		c = self.client
		c.put('/system/format', bare_format(fmt))
		key = (lambda f: (f.get('codec'), res_key(f.get('recordResolution')), f.get('frameRate'))) if need_resolution \
			else (lambda f: (f.get('codec'), f.get('frameRate')))
		want = key(fmt)
		deadline = time.monotonic() + timeout
		while True:
			now = c.format()
			if key(now) == want:
				self._device_format = now
				self._caps_dirty = True
				dr = fmt.get('_dynamicRange')
				if dr and c.dynamic_range() != dr:
					c.set_dynamic_range(dr)
					self._device_dr = dr
				return now
			if time.monotonic() > deadline:
				if key(now)[:1] == want[:1] and need_resolution:
					raise api.ApiError(0, 'the camera kept {} (resolution changes are ignored in vertical mode)'.format(
						res_key(now.get('recordResolution'))))
				raise api.ApiError(0, 'format change not confirmed by the camera')
			time.sleep(0.3)

	def _set_format_param(self, param, value):
		if param == 'colorspace':
			with self._format_lock:
				if self.live_mode:
					self.saved_format = dict(self.saved_format, _dynamicRange=str(value))
					self.manager._save_later()
				else:
					self.client.set_dynamic_range(value)
					self._device_dr = str(value)
			self._update(**self._format_state())
			return
		with self._format_lock:
			base = self.saved_format if self.live_mode else (self._device_format or self.client.format())
			kw = {'codec': None, 'resolution': None, 'fps': None}
			kw[param] = str(value)
			if param == 'resolution':
				# keep the codec if the new resolution offers it, else take its first one
				entry = self._entry(str(value)) or {}
				if base.get('codec') not in entry.get('codecs', [base.get('codec')]):
					kw['codec'] = entry['codecs'][0]
				if base.get('frameRate') not in entry.get('frameRates', [base.get('frameRate')]):
					kw['fps'] = entry['frameRates'][0]
			fmt = self._build_format(base, **kw)
			if base.get('_dynamicRange'):
				fmt['_dynamicRange'] = base['_dynamicRange']
			if self.live_mode:
				self.saved_format = fmt   # applied when live view ends or REC starts
				self.manager._save_later()
			else:
				self._apply_format(fmt)
		self._update(**self._format_state())

	def _enter_live_format(self):
		with self._format_lock:
			c = self.client
			current = c.format()
			if not self.live_mode:
				dr = self._try('colorspace', c.dynamic_range)
				self.saved_format = dict(current, **({'_dynamicRange': dr} if dr else {}))
				self.live_mode = True
				self.manager._save_later()
			hd = [f for f in self._supported if LIVE_HEIGHT in ((f.get('recordResolution') or {}).get('height'),
																 (f.get('recordResolution') or {}).get('width'))]
			if not hd:
				raise api.ApiError(0, 'camera offers no 1080p format for live view')
			entry = hd[0]
			codec = LIVE_CODEC if LIVE_CODEC in entry.get('codecs', []) else next(
				(x for x in entry.get('codecs', []) if x.startswith('H.264')), None)
			if not codec:
				raise api.ApiError(0, 'camera offers no H.264 format for live view')
			fps = self.saved_format.get('frameRate')
			fps = WHOLE_FPS.get(fps, fps)
			if fps not in entry.get('frameRates', [fps]):
				fps = '25' if '25' in entry.get('frameRates', []) else entry['frameRates'][0]
			live = self._build_format(current, codec=codec, resolution=res_key(entry['recordResolution']), fps=fps)
			same = (current.get('codec'), res_key(current.get('recordResolution')), current.get('frameRate')) == \
				(live['codec'], res_key(live['recordResolution']), live['frameRate'])
			if not same:
				try:
					c.stream(False)   # a stream running across a format change carries no video
				except api.ApiError:
					pass
				self._apply_format(live, need_resolution=False)   # H.264 is what the stream needs
		self._update(**self._format_state())

	def exit_live(self):
		"""Stop the live stream and restore the real recording format. Safe to
		call from any thread; a no-op when not in live mode."""
		with self._format_lock:
			if not self.live_mode:
				return
			c = self.client
			try:
				c.stream(False)
			except api.ApiError:
				pass
			self._apply_format(self.saved_format)
			self.live_mode = False
			self.saved_format = None
			self.manager._save_later()
		self._update(streaming='Idle', liveOurs=False, liveWarning=None, **self._format_state())
		self.manager._log('{}: live view ended, recording format restored'.format(self._label()))

	def _stream_warning(self, status, bitrate, video_format):
		if status != 'Streaming' or bitrate:
			self._zero_bitrate_since = None
			return None
		if self._zero_bitrate_since is None:
			self._zero_bitrate_since = time.monotonic()
		if time.monotonic() - self._zero_bitrate_since < 8:
			return None
		return ('The phone is streaming but sending no video ({}). The live stream only works while the '
				'recording codec is H.264: ProRes and HEVC block it.').format(video_format or '?')

	# --- commands ---------------------------------------------------------------

	def snap(self, param, value):
		"""A value the camera can take: out-of-range values are clamped to the
		range (WB, tint, iris, reel, zoom) or replaced by the nearest value the
		camera lists (ISO, shutter, frame rate, lens). Unparseable values pass
		through, to be refused when set."""
		s = self.state
		try:
			if param in ('wb', 'tint'):
				return int(round(clamp(float(value), s.get(param + 'Range'))))
			if param == 'iso':
				return int(nearest(float(value), s.get('isoOptions') or [], log=True))
			if param == 'shutter':
				parsed = parse_shutter(value, s.get('shutterMode'))
				if parsed.get('angle') is not None:
					raw = nearest(parsed['angle'] * self._angle_scale, self._shutter_options.get('shutterAngles') or [], log=True)
					return format_shutter({'shutterAngle': raw})
				return '1/{}'.format(int(nearest(parsed['speed'], self._shutter_options.get('shutterSpeeds') or [], log=True)))
			if param == 'iris':
				return format_f_stop(clamp(parse_f_stop(value), self._iris_range))
			if param == 'fps':
				opts = s.get('fpsOptions') or []
				if str(value) in opts or not opts:
					return str(value)
				return min(opts, key=lambda o: abs(float(o) - float(value)))
			if param == 'reel':
				return int(clamp(int(float(value)), REEL_RANGE))
			if param == 'lens':
				v = str(value).strip()
				names = lens_names(self._lenses)
				if any(l['id'] == v or names[l['id']].lower() == v.lower() for l in self._lenses):
					return v
				mm = float(v.lower().replace('mm', ''))
				back = [l for l in self._lenses if l.get('facing') != 'front' and l.get('focalLength')]
				if back:
					return nearest_lens(back, mm)['id']
				return int(round(clamp(mm, s.get('lensRange'))))
		except (ValueError, TypeError, ZeroDivisionError):
			pass
		return value

	def queue_set(self, param, value):
		"""Queue a setting; returns the value actually sent (see snap)."""
		if param != 'focus':
			value = self.snap(param, value)
		if param == 'focus':
			# Slider drags send many values: keep only the latest.
			with self.manager.lock:
				queued = self._focus_target is not None
				self._focus_target = value
			if queued:
				return value
		self.commands.put(('set', param, value))
		return value

	def queue(self, *cmd):
		self.commands.put(cmd)

	def _execute(self, kind, *args):
		c = self.client
		label = kind
		try:
			if kind == 'event':
				self._event(*args)
			elif kind == 'open':
				label = 'connecting'
				self._opened(*args)
			elif kind == 'closed':
				self._closed(*args)
			elif kind == 'refused':
				label = 'connecting'
				self._refused(*args)
			elif kind == 'set':
				param, value = args
				label = 'set {}'.format(param)
				self._set(param, value)
				self._read_back(param, value)
			elif kind == 'autowb':
				label = 'auto WB'
				c.auto_white_balance()
				self.release_wb_hold()
				self._read('wb', '/video/whiteBalance')
				self._read('tint', '/video/whiteBalanceTint')
			elif kind == 'stream':
				label = 'live view'
				if args[0]:
					self._enter_live_format()
					try:
						self._start_live()
					except api.ApiError:
						self.exit_live()   # don't leave the camera on the live format
						raise
				elif self.live_mode:
					self.exit_live()
				else:
					c.stream(False)
					self._update(streaming='Idle', liveOurs=False)
				self._read('stream', '/livestreams/0')
			elif kind == 'resync':
				label = 'reading the camera'
				self._resync(light=bool(args and args[0]))
			elif kind == 'refresh':
				self._reconnect()
		except api.ApiError as e:
			self._error('{} failed ({})'.format(label, e))
		except (ValueError, TypeError) as e:
			self._error('{}: bad value ({})'.format(label, e))

	def _read_back(self, param, value):
		"""After a command, read what the camera took."""
		if self.QUIET_DURING_TAKE and self._listening and self.state['recording']:
			return   # its events confirm
		for path in self.READ_BACK.get(param, ()):
			if path == '/lens/cameras/active' and not self._lenses:
				continue
			self._read(self.PATH_KEYS[path], path)
		if param in MONITOR_SWITCHES and isinstance(value, dict):
			display, (feature, key) = str(value.get('display')), MONITOR_SWITCHES[param]
			if display in self._monitor_displays[param]:
				on = self.client.monitor_switch(display, feature)
				self._update(**{key: dict(self.state.get(key) or {}, **{display: on})})
		if self._caps_dirty and param in ('fps', 'codec', 'resolution'):
			self._load_capabilities()   # the lists of supported values follow the format

	def _set(self, param, value):
		c = self.client
		if param in FOCUS_PARAMS and self.focus_locked:
			if param == 'focus':
				with self.manager.lock:
					self._focus_target = None
			raise api.ApiError(0, 'focus is locked')
		if param == 'focus':
			with self.manager.lock:
				value, self._focus_target = self._focus_target, None
			if value is None:
				return
			c.set_focus(value)
			self._update(focus=max(0.0, min(1.0, float(value))), autofocus='Off')
		elif param == 'autofocus':
			on = str(value).lower() in ('1', 'true', 'on', 'continuous')
			c.set_autofocus(on)
			self._update(autofocus='Continuous' if on else 'Off')
		elif param == 'focusonce':
			c.focus_once()
		elif param in MONITOR_SWITCHES:
			# value: {'display': 'HDMI', 'enabled': True}
			if not isinstance(value, dict) or 'display' not in value or 'enabled' not in value:
				raise ValueError('expected {"display": ..., "enabled": ...}')
			display, enabled = str(value['display']), bool(value['enabled'])
			feature, key = MONITOR_SWITCHES[param]
			if display not in self._monitor_displays[param]:
				raise ValueError('no {} switch for display {}'.format(feature, display))
			c.set_monitor_switch(display, feature, enabled)
			self._update(**{key: dict(self.state.get(key) or {}, **{display: enabled})})
			if param == 'cleanfeed' and display == 'HDMI':
				self._hdmi_video_feed()
		elif param == 'focuslock':
			lock = str(value).lower() in ('1', 'true', 'on', 'lock', 'locked')
			if lock and self.state.get('autofocus') != 'Off':
				c.set_autofocus(False)   # hold focus where it is
				self._update(autofocus='Off')
			self.focus_hold = c.focus() if lock else None
			self.focus_locked = lock
			self._focus_restoring = False
			self._update(focusLocked=lock, **({'focus': self.focus_hold} if self.focus_hold is not None else {}))
			self.manager._save_later()
		elif param == 'iso':
			self._lock_exposure()
			c.set_iso(int(float(value)))
			self._update(iso=int(float(value)))
		elif param == 'shutter':
			parsed = parse_shutter(value, self.state['shutterMode'])
			angle = parsed.get('angle')
			if angle is not None:
				# the listed value exactly ('261.818°' is shown for 261.8181818...)
				angle = nearest(angle * self._angle_scale, self._shutter_options.get('shutterAngles') or [], log=True)
			self._lock_exposure()
			c.set_shutter(speed=parsed.get('speed'), angle=angle)
		elif param == 'wb':
			c.set_white_balance(int(float(value)))
			self._hold_wb('wb', int(float(value)))
		elif param == 'tint':
			c.set_tint(int(float(value)))
			self._hold_wb('tint', int(float(value)))
		elif param in ('fps', 'codec', 'resolution', 'colorspace'):
			self._set_format_param(param, value)
		elif param == 'iris':
			if not self._iris_controllable:
				raise api.ApiError(0, 'this camera has a fixed aperture')
			f_stop = parse_f_stop(value)
			self._lock_exposure()   # the phone refuses iris changes under auto exposure
			try:
				c.set_iris(f_stop)
			except api.ApiError as e:
				if e.status == 403:
					raise api.ApiError(403, 'iris is set by auto exposure, or not controllable right now')
				raise
			self._update(iris=format_f_stop(f_stop))
		elif param == 'lens':
			self._set_lens(value)
		elif param == 'letter':
			c.set_camera_letter(str(value).strip())
			self._update(letter=str(value).strip())
		elif param == 'reel':
			c.set_reel(int(float(value)))
			self._update(reel=int(float(value)))
		else:
			raise ValueError('unknown parameter ' + param)

	# --- locks: a value set from here stays put -----------------------------------

	def _lock(self, what, fn):
		"""Best effort: the value itself is set either way."""
		try:
			fn()
		except api.ApiError as e:
			if e.status == 0:
				raise
			self._error('could not lock {} ({})'.format(what, e))

	def _lock_exposure(self):
		"""Auto exposure off, so ISO, shutter and iris keep the values set from here.
		(App 3.5 already drops AE when ISO or shutter is set; iris is refused under AE.)"""
		def off():
			ae = self._try('autoexposure', self.client.auto_exposure)
			if ae and ae.get('mode') not in (None, 'Off'):
				self.client.set_auto_exposure('Off')
				self._update(irisAuto=False)
		self._lock('exposure', off)

	def _lock_lens(self):
		"""Automatic lens switching off, so the phone stays on the lens picked here."""
		def off():
			al = self._try('autolens', self.client.auto_lens)
			if al and al.get('supported') and al.get('enabled'):
				self.client.set_auto_lens(False)
		self._lock('lens', off)

	def _hold_wb(self, key, value):
		# hold what the phone actually took (it may clamp)
		read = self._try(key, self.client.white_balance if key == 'wb' else self.client.tint)
		held = read if read is not None else value
		self.wb_hold[key] = held
		self._wb_restores = 0
		self._update(**{key: held, 'wbHold': dict(self.wb_hold)})
		self.manager._save_later()

	def release_wb_hold(self):
		if self.wb_hold:
			self.wb_hold = {}
			self._update(wbHold={})
			self.manager._save_later()

	def _holding(self, key):
		return bool(self.wb_hold) if key == 'wb' else self.focus_locked and self.focus_hold is not None

	def _enforce_holds(self, now):
		"""Put back a held white balance or a locked focus the phone has changed. Driven by
		the camera's events; each restore is one write, no more often than the intervals
		above."""
		for key in list(self._hold_check):
			if not self._holding(key):
				self._hold_check.discard(key)
				continue
			if now < self._hold_after[key] or not self._may_send():
				continue
			self._hold_check.discard(key)
			try:
				if key == 'wb' and self._restore_wb():
					self._hold_after[key] = now + self.WB_RESTORE_EVERY
				elif key == 'focus' and self._restore_focus():
					self._hold_after[key] = now + self.FOCUS_RESTORE_EVERY
			except api.ApiError:
				self._hold_check.add(key)   # unanswered: still owed, after the back-off
				raise

	def _restore_wb(self):
		"""Put back a held WB/tint the phone has changed (auto WB, someone on the phone).
		Returns whether anything was sent."""
		drifted = False
		upd = {}
		for key, setter, getter in (('wb', self.client.set_white_balance, self.client.white_balance),
									('tint', self.client.set_tint, self.client.tint)):
			held, now = self.wb_hold.get(key), self.state.get(key)
			if held is None or now is None or now == held:
				continue
			drifted = True
			try:
				setter(held)
				upd[key] = getter()
			except api.ApiError as e:
				if e.status == 0:
					raise
				self._error('could not restore held {} ({})'.format(key, e))
				continue
			if upd[key] != held:   # the phone won't take it any more: hold what it has
				self.wb_hold[key] = upd[key]
				self.manager._save_later()
			self.manager._log('{}: {} changed on the phone ({} -> {}), restored'.format(
				self._label(), 'white balance' if key == 'wb' else 'tint', held, now))
		if drifted:
			if time.monotonic() - self._wb_last_restore > 4 * self.WB_RESTORE_EVERY:
				self._wb_restores = 0   # it had stayed put for a while
			self._wb_last_restore = time.monotonic()
			self._wb_restores += 1
			if self._wb_restores == 3:
				self._error('white balance keeps changing on the phone: turn off auto white balance in the app')
			self._update(wbHold=dict(self.wb_hold), **upd)
		return drifted

	def _restore_focus(self):
		"""While focus is locked, put it back if it moved. Returns whether anything was sent."""
		focus = self.state.get('focus')
		if focus is None or self.focus_hold is None:
			return False
		if abs(focus - self.focus_hold) < 1.5 / 255:   # the phone steps in 1/255 and may read back one off
			self._focus_restoring = False
			return False
		try:
			self.client.set_focus(self.focus_hold)   # also ends continuous AF
		except api.ApiError as e:
			if e.status == 0:
				raise
			self._error('could not restore locked focus ({})'.format(e))
			return True
		if not self._focus_restoring:   # one log line per disturbance, not per restore
			self._focus_restoring = True
			self.manager._log('{}: focus moved on the phone ({:.0f}% -> {:.0f}%), restored'.format(
				self._label(), self.focus_hold * 100, focus * 100))
		self._update(focus=self.focus_hold, autofocus='Off')
		return True

	HDMI_VIDEO_FEED = 4   # appsettings.hdmiOut in presets: 0 = mirror display, 4 = video feed

	def _hdmi_video_feed(self):
		"""Set the app's HDMI output to "Video feed" (not "Mirror display"), so the
		clean feed switch has an effect. The API has no setting for it, but presets
		carry it: apply a copy of the current settings with hdmiOut changed."""
		c = self.client
		if self.state['recording']:
			raise api.ApiError(0, 'clean feed set, but HDMI can only be switched to video feed when not recording')
		tag = 'lucam-' + uuid.uuid4().hex[:6]
		made = []
		focus = self._try('focus', c.focus)
		try:
			c.save_preset(tag)
			made.append(tag)
			settings = json.loads(c.download_preset(tag)[0])
			app = settings.get('appsettings') or {}
			if app.get('hdmiOut') == self.HDMI_VIDEO_FEED:
				return
			app['hdmiOut'] = self.HDMI_VIDEO_FEED
			# The saved focus can be wrong (0 when saved while the app's menu is
			# open), and applying it with the app's focus lock on froze focus
			# there: use the position the API reports.
			sc = settings.get('secondaryControlsData')
			if focus is not None and isinstance(sc, dict) and 'focusValue' in sc:
				sc['focusValue'] = focus
			copy = c.upload_preset(tag + '-hdmi.cset', json.dumps(settings).encode())
			made.append(copy)
			c.apply_preset(copy)
			# The copy's focus can be off (e.g. saved while the app's menu was
			# open): put focus back where it was.
			if focus is not None and self._try('focus', c.focus) != focus:
				c.set_focus(focus)
		except ValueError as e:
			raise api.ApiError(0, 'could not switch HDMI to video feed ({})'.format(e))
		finally:
			for name in made:
				try:
					c.delete_preset(name)
				except api.ApiError:
					pass

	def _set_lens(self, value):
		"""A lens id from the phone's list, or a focal length in mm: picks the
		back lens with that focal length, else zooms (phones without /lens/cameras)."""
		c = self.client
		value = str(value).strip()
		lens = next((l for l in self._lenses if l['id'] == value), None)
		if lens is None:   # a lens name: 'Front', 'Front ultra wide'...
			names = lens_names(self._lenses)
			lens = next((l for l in self._lenses if names[l['id']].lower() == value.lower()), None)
		if lens is None and self._lenses:
			mm = float(value.lower().replace('mm', ''))
			lens = next((l for l in self._lenses if l.get('facing') != 'front' and l.get('focalLength') == mm), None)
		if lens is not None:
			self._lock_lens()
			c.set_active_lens(lens['id'])
			self._update(lensId=lens['id'], lens=lens_names(self._lenses)[lens['id']] if lens.get('facing') == 'front'
						 else lens.get('focalLength'))
		else:
			mm = int(float(value.lower().replace('mm', '')))
			c.set_focal_length(mm)
			self._update(lens=mm)

	def do_record(self, recording, go, skip_reason=None):
		"""Runs on a short-lived thread; `go` is a shared Event so every camera
		fires as close to simultaneously as possible. With an event channel the
		tally follows the camera's own word, and a REC or STOP it acknowledged
		without carrying out is reported."""
		self._update(recordPending=True)
		go.wait(2.0)
		if skip_reason:
			self._error('REC skipped: ' + skip_reason)
			self._update(recordPending=False)
			return
		recording = bool(recording)
		what = 'REC' if recording else 'STOP'
		try:
			with self.manager.lock:
				self._rec_confirm.clear()
				self._rec_expected = recording
				if recording:
					self._rec_started = time.time()
					self.state['stopAlert'] = None
				else:
					self._expect_stop_until = time.monotonic() + 10
			self.client.record(recording)
			if not self._listening:
				self._update(recording=recording)
			elif not self._rec_confirm.wait(self.REC_CONFIRM):
				# No event: ask once.
				try:
					actual = self.client.is_recording()
				except api.ApiError as e:
					actual = None
					self._error('{} sent; the camera has not confirmed it ({})'.format(what, e))
				if actual is not None and actual != recording:
					msg = '{} ACKNOWLEDGED BUT NOT CARRIED OUT: the camera is {} recording'.format(
						what, 'still' if actual else 'not')
					self._error(msg)
					self.manager.alert('{}: {}'.format(self._label(), msg))
				if actual is not None:
					with self.manager.lock:
						self.state['recording'] = actual
						self._rec_reported = actual
		except api.ApiError as e:
			self._error('{} failed ({})'.format(what, e))
		finally:
			self._rec_expected = None
			self._update(recordPending=False)
			self.commands.put(('wake',))


class CameraManager:
	def __init__(self, config_path=None, web_dir=WEB_DIR, autosave=True, log_dir=None, log_requests=True,
				 log_keep_days=30):
		self.lock = threading.RLock()
		self.config_path = config_path
		self.web_dir = web_dir
		self.autosave = autosave
		self.workers = {}          # id -> CameraWorker, in insertion order
		self.global_values = {}
		self.log = []
		# log_dir: folder for the run's log files (None: in-memory log only, as in the panel)
		self.files = filelog.FileLog(log_dir, requests=log_requests, keep_days=log_keep_days) if log_dir else None
		self._by_address = {}   # (host, port) -> worker, for naming requests in the log
		if self.files:
			api.request_observer = self._observe_request
		self.discovery = {'running': False, 'mode': None, 'results': [], 'error': None, 'finished': 0}
		self._save_timer = None
		self.live = livestream.LiveServer()
		self.notify = True                      # system notification on alarms (macOS), silent
		self.settings = {'interface': 'auto'}   # network interface for discovery / live view
		self.http_port = None                   # set by server.py, for the panel address
		self._load()

	# --- persistence ------------------------------------------------------------

	def _load(self):
		if not self.config_path or not os.path.exists(self.config_path):
			return
		try:
			with open(self.config_path) as f:
				cfg = json.load(f)
		except (OSError, ValueError) as e:
			self._log('could not read {}: {}'.format(self.config_path, e))
			return
		self.global_values = cfg.get('global', {})
		self.settings.update(cfg.get('settings', {}))
		for cam in cfg.get('cameras', []):
			try:
				self._spawn(cam.get('id') or uuid.uuid4().hex[:8], cam['address'],
							cam.get('name', ''), cam.get('active', True),
							cam.get('liveMode', False), cam.get('savedFormat'), cam.get('focusLocked', False),
						cam.get('wbHold'), cam.get('focusHold'))
			except (KeyError, ValueError) as e:
				self._log('skipping saved camera: {}'.format(e))

	def save(self):
		if not self.config_path:
			return
		with self.lock:
			cfg = {
				'cameras': [{'id': w.state['id'],
							 'address': w.state['address'] if w.resolved else w.typed_address,
							 'name': w.state['name'], 'active': w.state['active'],
							 **({'liveMode': True, 'savedFormat': w.saved_format} if w.live_mode else {}),
							 **({'focusLocked': True, 'focusHold': w.focus_hold} if w.focus_locked else {}),
						 **({'wbHold': w.wb_hold} if w.wb_hold else {})}
							for w in self.workers.values()],
				'global': self.global_values,
				'settings': self.settings,
			}
		tmp = self.config_path + '.tmp'
		with open(tmp, 'w') as f:
			json.dump(cfg, f, indent=2)
		os.replace(tmp, self.config_path)

	def _save_later(self):
		if not self.autosave or not self.config_path:
			return
		if self._save_timer:
			self._save_timer.cancel()
		self._save_timer = threading.Timer(0.5, self.save)
		self._save_timer.daemon = True
		self._save_timer.start()

	def _log(self, msg):
		t = time.time()
		with self.lock:
			self.log.append({'t': t, 'msg': msg})
			del self.log[:-50]
		if self.files:
			self.files.event(msg, t)

	def alert(self, msg):
		"""Something the operator must see now: logged with an ALERT prefix and, on a Mac,
		shown as a system notification (no sound) so it is seen even with the panel in
		the background. `notify` turns the notification off."""
		self._log('ALERT ' + msg)
		if not self.notify or sys.platform != 'darwin':
			return
		def quote(s):
			return s.replace('\\', '\\\\').replace('"', '\\"')
		title, _, text = msg.partition(': ')
		script = 'display notification "{}" with title "LUCam: {}"'.format(
			quote(text[:200] or msg[:200]), quote(title[:40]))
		try:
			subprocess.Popen(['osascript', '-e', script], stdin=subprocess.DEVNULL,
							 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		except OSError:
			pass

	def _observe_request(self, sent_at, host, port, method, path, status, took, error, sent):
		"""api.request_observer: every request any worker sends, written to the request log
		under the camera's name (or its address while it has none)."""
		w = self._by_address.get((host, port))
		if w is None or w.client.host != host or w.client.port != port:
			# a camera added, or one now reached on another address: rebuild the map
			with self.lock:
				self._by_address = {(x.client.host, x.client.port): x for x in self.workers.values()}
			w = self._by_address.get((host, port))
		name = (w.state['name'] or w.state['deviceName'] or host) if w else host
		self.files.request(sent_at, name, method, path, status, took, error, sent)

	# --- cameras ------------------------------------------------------------------

	def _spawn(self, cam_id, address, name, active, live_mode=False, saved_format=None, focus_locked=False,
			   wb_hold=None, focus_hold=None):
		w = CameraWorker(self, cam_id, address, name, active, live_mode, saved_format, focus_locked, wb_hold,
						 focus_hold)
		with self.lock:
			self.workers[cam_id] = w
		w.start()
		return w

	def add_camera(self, address, name=''):
		candidates = api.address_candidates(address)
		with self.lock:
			for w in self.workers.values():
				if w.state['address'] in candidates or w.typed_address == address.strip():
					raise ValueError('{} is already in the list'.format(w.state['address']))
		w = self._spawn(uuid.uuid4().hex[:8], address.strip(), name.strip(), True)
		self._save_later()
		return w.state['id']

	def remove_camera(self, cam_id):
		with self.lock:
			w = self.workers.pop(cam_id, None)
		if w:
			w.stop()
			self._save_later()

	def update_camera(self, cam_id, name=None, active=None):
		w = self._worker(cam_id)
		with self.lock:
			if name is not None:
				w.state['name'] = str(name).strip()
			if active is not None:
				w.state['active'] = bool(active)
		self._save_later()

	def _worker(self, cam_id):
		with self.lock:
			w = self.workers.get(cam_id)
		if not w:
			raise KeyError('unknown camera ' + str(cam_id))
		return w

	def active_ids(self):
		with self.lock:
			return [k for k, w in self.workers.items() if w.state['active']]

	# --- commands -----------------------------------------------------------------

	def set_param(self, cam_id, param, value):
		if param not in PARAMS:
			raise ValueError('unknown parameter ' + str(param))
		return self._worker(cam_id).queue_set(param, value)

	def set_global(self, param, value):
		"""Push one value to every active camera, overriding individual values.
		Returns what each camera was sent (out-of-range values snapped per camera)."""
		if param not in PARAMS:
			raise ValueError('unknown parameter ' + str(param))
		with self.lock:
			# a fixed-aperture camera just keeps its iris
			skip = {k for k, w in self.workers.items() if param == 'iris' and not w.state.get('irisControllable')}
		sent = {}
		for cam_id in self.active_ids():
			if cam_id not in skip:
				sent[cam_id] = self.set_param(cam_id, param, value)
		with self.lock:
			# the global row shows what the cameras got (the first one's, if they differ)
			self.global_values[param] = next(iter(sent.values()), value)
		self._save_later()
		return sent

	def auto_wb(self, cam_ids):
		for cam_id in cam_ids:
			self._worker(cam_id).queue('autowb')

	def stream(self, cam_ids, enabled):
		for cam_id in cam_ids:
			self._worker(cam_id).queue('stream', bool(enabled))

	def stop_all_live(self):
		"""End every live view (armed or not): stop the stream and restore each
		camera's real recording format. Returns the number of cameras affected."""
		with self.lock:
			live = [w for w in self.workers.values() if w.live_mode or w.state.get('liveOurs')]
		for w in live:
			w.queue('stream', False)
		return len(live)

	def refresh(self, cam_id):
		self._worker(cam_id).queue('refresh')

	def record(self, cam_ids, recording):
		"""Start/stop recording on several cameras at once. Returns immediately.

		Starting a recording first takes every camera out of live view and
		restores its real recording format, so nothing records the temporary
		H.264 live format."""
		workers = [self._worker(i) for i in cam_ids]
		with self.lock:
			live = [w for w in self.workers.values() if w.live_mode] if recording else []
		for w in workers:
			w._update(recordPending=True)

		def run():
			failed = self._exit_live_all(live)
			go = threading.Event()
			for w in workers:
				reason = failed.get(w.state['id'])
				threading.Thread(target=w.do_record, args=(recording, go, reason), daemon=True).start()
			go.set()

		threading.Thread(target=run, name='lucam-record', daemon=True).start()

	def _exit_live_all(self, workers):
		"""Restore formats in parallel; returns {cam_id: error} for failures."""
		failed = {}

		def one(w):
			try:
				w.exit_live()
			except api.ApiError as e:
				failed[w.state['id']] = 'could not restore the recording format ({})'.format(e)
				w._error(failed[w.state['id']])

		threads = [threading.Thread(target=one, args=(w,), daemon=True) for w in workers]
		for t in threads:
			t.start()
		for t in threads:
			t.join(15)
		return failed

	def record_all(self, recording):
		self.record(self.active_ids(), recording)

	# --- discovery ----------------------------------------------------------------

	def discover(self, mode='bonjour'):
		with self.lock:
			if self.discovery['running']:
				return
			self.discovery = {'running': True, 'mode': mode, 'results': [], 'error': None, 'finished': 0}

		def job():
			try:
				ifaces = self._discovery_interfaces()
				if mode == 'scan':
					results = discovery.discover_subnet(networks=['{}/{}'.format(i['ip'], i['prefix']) for i in ifaces])
				else:
					results = discovery.discover_bonjour(ips=[i['ip'] for i in ifaces])
				err = None
			except Exception as e:
				results, err = [], str(e)
			with self.lock:
				self.discovery.update(running=False, results=results, error=err, finished=time.time())

		threading.Thread(target=job, name='lucam-discovery', daemon=True).start()

	# --- network interfaces ---------------------------------------------------------

	def _selected_interface(self, interfaces=None):
		"""The chosen interface dict, None for 'auto'. Raises ValueError if the
		chosen interface isn't connected right now."""
		name = self.settings.get('interface', 'auto')
		if name == 'auto':
			return None
		for iface in interfaces if interfaces is not None else discovery.list_interfaces():
			if iface['name'] == name:
				return iface
		raise ValueError('network interface {} is not connected'.format(name))

	def _discovery_interfaces(self):
		"""Interfaces to search for cameras: the selected one, or every non-VPN one."""
		ifaces = discovery.list_interfaces()
		selected = self._selected_interface(ifaces)
		if selected:
			return [selected]
		return [i for i in ifaces if not i['vpn']] or ifaces

	def live_address_for(self, camera_host):
		"""This Mac's address a camera should stream to: the selected interface's
		when the camera is on its network, else the one the OS routes through."""
		try:
			selected = self._selected_interface()
		except ValueError:
			selected = None
		if selected and discovery.interface_for(camera_host, [selected]):
			return selected['ip']
		return livestream.local_ip_toward(camera_host)

	def network_info(self):
		ifaces = discovery.list_interfaces()
		with self.lock:
			cams = [(w.state['name'] or w.state['address'], w.client.host) for w in self.workers.values()]
		via = []
		for name, host in cams:
			iface = discovery.interface_for(host, ifaces)
			try:
				local = livestream.local_ip_toward(host)
			except OSError:
				local = None
			if iface is None:   # not on a local network: report the routed interface
				iface = next((i for i in ifaces if i['ip'] == local), None)
			via.append({'camera': name, 'host': host, 'interface': iface and iface['name'],
						'label': iface and iface['label'], 'localIp': local})
		selected = self.settings.get('interface', 'auto')
		return {
			'interfaces': ifaces,
			'selected': selected,
			'selectedConnected': selected == 'auto' or any(i['name'] == selected for i in ifaces),
			'port': self.http_port,
			'cameras': via,
		}

	def set_interface(self, name):
		name = str(name or 'auto')
		if name != 'auto' and not any(i['name'] == name for i in discovery.list_interfaces()):
			raise ValueError('unknown network interface ' + name)
		with self.lock:
			self.settings['interface'] = name
		self._save_later()
		self._log('network interface set to ' + name)

	# --- state ----------------------------------------------------------------------

	def snapshot(self):
		with self.lock:
			workers = list(self.workers.values())
			disc = dict(self.discovery)
			addresses = {w.state['address'] for w in workers}
			disc['results'] = [dict(r, added=r['address'] in addresses) for r in disc['results']]
			glob = dict(self.global_values)
			log = list(self.log[-10:])
		# Sorted by camera letter (A, B, C...), then name; cameras without a
		# letter yet (offline, connecting) go last.
		cams = sorted((w.snapshot() for w in workers), key=lambda c: (
			not (c['letter'] or '').strip(), (c['letter'] or '').strip().upper(), (c['name'] or c['address']).lower()))
		paths = self.live.paths() if any(c['liveOurs'] for c in cams) else {}
		for c in cams:
			p = paths.get('live/' + c['id'])
			c['liveReady'] = bool(p and p['ready'])
		return {
			'cameras': cams,
			'global': glob,
			'discovery': disc,
			'log': log,
			'anyRecording': any(c['recording'] for c in cams),
			'time': time.time(),
		}

	def shutdown(self):
		with self.lock:
			live = [w for w in self.workers.values() if w.live_mode and w.state['online']]
		if live:
			self._exit_live_all(live)  # never leave a phone recording the live format
		if self._save_timer:
			self._save_timer.cancel()
			self.save()
		with self.lock:
			workers = list(self.workers.values())
			self.workers.clear()
		for w in workers:
			w.stop()
		self.live.stop()
		if self.files:
			api.request_observer = None
			self.files.event('quit')
			self.files.close()

	# --- HTTP API -------------------------------------------------------------------

	def handle_http(self, method, path, body=b''):
		"""Request handler used by server.py.
		Returns (status, content_type, bytes, extra_headers)."""
		status, ctype, data, *extra = self._handle_http(method, path, body)
		return status, ctype, data, (extra[0] if extra else {})

	def _handle_http(self, method, path, body):
		path, _, query = path.partition('?')
		m = re.match(r'^/api/cameras/([^/]+)/presets(?:/([a-z]+))?$', path)
		if m:
			return self._presets(method, m.group(1), m.group(2), dict(urllib.parse.parse_qsl(query)), body)
		if method == 'GET' and path in ('/', '/index.html'):
			return self._static('index.html')
		if method == 'GET' and (path == '/live' or path.startswith('/live/')):
			return self._static('live.html')
		if not path.startswith('/api/'):
			return 404, 'text/plain', b'Not found'
		try:
			payload = json.loads(body.decode('utf-8')) if body else {}
		except ValueError:
			return 400, 'application/json', b'{"error": "invalid JSON"}'
		try:
			result = self._route(method, path[len('/api'):], payload)
			return 200, 'application/json', json.dumps(result if result is not None else {'ok': True}).encode()
		except KeyError as e:
			return 404, 'application/json', json.dumps({'error': str(e).strip("'")}).encode()
		except (ValueError, TypeError) as e:
			return 400, 'application/json', json.dumps({'error': str(e)}).encode()

	PRESET_EXTENSIONS = ('.cset', '.bmcpreset')

	def _presets(self, method, cam_id, action, query, body):
		"""Preset files go straight to/from the camera (binary, so outside _route)."""
		def as_json(status, obj):
			return status, 'application/json', json.dumps(obj).encode()
		try:
			client = self._worker(cam_id).client
			if method == 'GET' and action is None:
				return as_json(200, {'presets': client.presets(), 'active': client.active_preset()})
			if method == 'GET' and action == 'download':
				data, filename = client.download_preset(query['name'])
				quoted = urllib.parse.quote(filename)
				return 200, 'application/octet-stream', data, {
					'Content-Disposition': "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(
						filename.encode('ascii', 'replace').decode().replace('"', "'"), quoted)}
			if method == 'POST' and action == 'upload':
				filename = os.path.basename(query.get('filename', '')).strip()
				if not filename.lower().endswith(self.PRESET_EXTENSIONS):
					raise ValueError('preset files end in .cset or .bmcpreset')
				if not body or len(body) > 2 * 1024 * 1024:
					raise ValueError('empty or oversized preset file')
				return as_json(200, {'presetAdded': client.upload_preset(filename, body)})
			if method == 'POST' and action in ('save', 'apply', 'delete'):
				name = str(json.loads(body.decode('utf-8') or '{}').get('name', '')).strip()
				if not name or re.search(r'[\x00-\x1f"/:*?<>|\\]', name):
					raise ValueError('invalid preset name')
				getattr(client, action + '_preset')(name)
				if action == 'apply':
					self._worker(cam_id).release_wb_hold()   # the preset brings its own white balance
					self._worker(cam_id).queue('resync')     # and can change any other setting
				return as_json(200, {'ok': True})
			return as_json(404, {'error': 'no route'})
		except KeyError as e:
			return as_json(404 if 'camera' in str(e) else 400, {'error': 'missing ' + str(e).strip("'")})
		except (ValueError, TypeError) as e:
			return as_json(400, {'error': str(e)})
		except api.ApiError as e:
			if e.status == 404 and action in ('download', 'apply', 'delete'):
				return as_json(404, {'error': 'preset not found on the camera'})
			return as_json(502, {'error': 'camera: {}'.format(e)})

	def _static(self, name):
		try:
			with open(os.path.join(self.web_dir, name), 'rb') as f:
				return 200, 'text/html; charset=utf-8', f.read()
		except OSError:
			return 404, 'text/plain', b'web UI file missing: ' + name.encode()

	def _route(self, method, path, p):
		parts = [x for x in path.split('/') if x]
		if method == 'GET' and parts == ['state']:
			return self.snapshot()
		if parts == ['network']:
			if method == 'POST' and 'interface' in p:
				self.set_interface(p['interface'])
			return self.network_info()
		if method != 'POST':
			raise KeyError('no route')
		if parts == ['cameras', 'add']:
			return {'id': self.add_camera(p['address'], p.get('name', ''))}
		if parts == ['discover']:
			self.discover(p.get('mode', 'bonjour'))
			return None
		if parts == ['global', 'set']:
			return {'ok': True, 'values': self.set_global(p['param'], p['value'])}
		if parts == ['global', 'record']:
			self.record_all(bool(p['recording']))
			return None
		if parts == ['global', 'autowb']:
			self.auto_wb(self.active_ids())
			return None
		if parts == ['global', 'stream']:
			self.stream(self.active_ids(), p['enabled'])
			return None
		if parts == ['global', 'stoplive']:
			return {'stopped': self.stop_all_live()}
		if parts == ['global', 'active']:
			for cam_id in list(self.workers):
				self.update_camera(cam_id, active=bool(p['active']))
			return None
		if len(parts) == 3 and parts[0] == 'cameras':
			cam_id, action = parts[1], parts[2]
			if action == 'remove':
				self.remove_camera(cam_id)
			elif action == 'update':
				self.update_camera(cam_id, p.get('name'), p.get('active'))
			elif action == 'set':
				return {'ok': True, 'value': self.set_param(cam_id, p['param'], p['value'])}
			elif action == 'record':
				self.record([cam_id], bool(p['recording']))
			elif action == 'autowb':
				self.auto_wb([cam_id])
			elif action == 'stream':
				self.stream([cam_id], p['enabled'])
			elif action == 'refresh':
				self.refresh(cam_id)
			elif action == 'resync':
				# read the values the event channel does not carry (a row was expanded)
				self._worker(cam_id).queue('resync', True)
			else:
				raise KeyError('no route')
			return None
		raise KeyError('no route')
