"""Camera fleet manager: one worker thread per camera, global commands,
discovery, persistence, and the JSON HTTP API the web UI talks to.

Nothing here blocks the caller: every network call runs on a worker thread,
so HTTP request handlers only ever read snapshots and queue commands.
"""

import json
import os
import queue
import re
import threading
import time
import urllib.parse
import uuid

from . import api, discovery, livestream

PARAMS = ('lens', 'iso', 'fps', 'shutter', 'wb', 'tint', 'letter', 'reel', 'codec', 'resolution', 'colorspace')

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
	POLL_FAST = 0.5      # record state / timecode
	POLL_FULL = 3.0      # all settings
	RETRY_OFFLINE = 2.0

	def __init__(self, manager, cam_id, address, name='', active=True, live_mode=False, saved_format=None):
		super().__init__(name='lucam-' + cam_id, daemon=True)
		self.manager = manager
		# Live mode: the camera temporarily records H.264 1080p so it can stream;
		# `saved_format` is its real recording format, restored on exit and before REC.
		self.live_mode = bool(live_mode and saved_format)
		self.saved_format = saved_format if self.live_mode else None
		self._format_lock = threading.Lock()
		self._supported = []
		self._lenses = []
		self._device_format = None
		self._device_dr = None
		# Until one answers, cycle through the variants of what the user typed
		# (e.g. bare IP -> https://ip:4444, http://ip, https://ip).
		self.typed_address = address
		self.candidates = api.address_candidates(address)
		self.resolved = len(self.candidates) == 1
		self.client = api.CameraClient(self.candidates[0])
		self.commands = queue.Queue()
		self._halt = threading.Event()
		self._next_fast = 0.0
		self._next_full = 0.0
		self._caps_dirty = True
		self.unsupported = set()
		self._shutter_options = {}
		self._angle_scale = 1
		self._zero_bitrate_since = None
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
			'timecode': None,
			'streaming': None, 'streamBitrate': 0, 'liveOurs': False, 'livePath': None, 'liveWarning': None,
			'lens': None, 'lensOptions': [], 'lensRange': None, 'lensNative': False, 'lensMain': None,
			'lensId': None,
			'iso': None, 'isoOptions': [],
			'fps': None, 'fpsOptions': [],
			'codec': None, 'codecOptions': [], 'resolution': None, 'resolutionOptions': [],
			'colorspace': None, 'colorspaceOptions': [],
			'liveMode': self.live_mode, 'deviceFormat': None,
			'shutter': None, 'shutterMode': None, 'shutterOptions': [],
			'wb': None, 'wbRange': None,
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

	def _error(self, msg):
		self._update(error=msg, errorTime=time.time())
		self.manager._log('{}: {}'.format(self.state['name'] or self.state['address'], msg))

	def stop(self):
		self._halt.set()
		self.commands.put(None)

	# --- main loop ------------------------------------------------------------

	def run(self):
		while not self._halt.is_set():
			now = time.monotonic()
			wait = max(0.0, min(self._next_fast, self._next_full) - now)
			try:
				cmd = self.commands.get(timeout=wait)
			except queue.Empty:
				cmd = False
			if self._halt.is_set():
				break
			if cmd:
				self._execute(*cmd)
				if time.monotonic() < min(self._next_fast, self._next_full):
					continue
			self._poll()

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

	def _poll(self):
		now = time.monotonic()
		c = self.client
		try:
			if self._caps_dirty or not self.state['online']:
				self._load_capabilities()
			rec = self._try('record', c.is_recording)
			if not self.resolved:
				self.resolved = True
				self.manager._save_later()
			upd = {'online': True, 'connecting': False, 'lastSeen': time.time()}
			if rec is not None and not self.state['recordPending']:
				upd['recording'] = rec
			tc = self._try('timecode', c.timecode)
			if tc is not None:
				upd['timecode'] = tc
			self._update(**upd)
			if now >= self._next_full:
				self._poll_settings()
				self._next_full = now + self.POLL_FULL
			self._next_fast = now + self.POLL_FAST
		except api.ApiError as e:
			if not self.resolved and self._try_other_addresses():
				self._next_fast = self._next_full = 0
				return
			if self.state['online'] or self.state['connecting']:
				self._error('offline ({})'.format(e))
			self._update(online=False, connecting=False)
			self._caps_dirty = True
			self._next_fast = self._next_full = now + self.RETRY_OFFLINE

	def _start_live(self):
		"""Point the camera's live stream at our MediaMTX and start it."""
		c = self.client
		try:
			self.manager.live.ensure_running()
		except RuntimeError as e:
			raise api.ApiError(0, str(e))
		rtmp_url = 'rtmp://{}:{}/live'.format(livestream.local_ip_toward(c.host), livestream.RTMP_PORT)
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
			c.upload_platform(livestream.PLATFORM_FILE, livestream.platform_xml(rtmp_url))
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
		self._update(livePath='live/' + key, liveOurs=True, streaming='Connecting')
		self.manager._log('{}: live view started'.format(self.state['name'] or self.state['address']))

	def _reconnect(self):
		"""Drop everything learned about the camera and connect from scratch:
		fresh client, re-resolve the address, re-read capabilities."""
		self.unsupported.clear()
		self._shutter_options = {}
		self._angle_scale = 1
		self.resolved = len(self.candidates) == 1
		self.client = api.CameraClient(self.candidates[0] if not self.resolved else self.state['address'])
		self._caps_dirty = True
		self._update(connecting=True, online=False, error=None, errorTime=0, recordPending=False)
		self.manager._log('{}: reconnecting'.format(self.state['name'] or self.state['address']))
		self._next_fast = self._next_full = 0

	def _try_other_addresses(self):
		for candidate in self.candidates[1:]:
			if api.probe(candidate, timeout=1.5) is not None:
				self.client = api.CameraClient(candidate)
				self.resolved = True
				self._update(address=self.client.address)
				self.manager._save_later()
				return True
		return False

	def _load_capabilities(self):
		c = self.client
		info = self._try('product', c.product) or {}
		upd = {
			'product': info.get('productName', ''),
			'deviceName': info.get('deviceName', ''),
			'software': info.get('softwareVersion', ''),
		}
		if not self.state['name'] and upd['deviceName']:
			upd['name'] = upd['deviceName']
			self.manager._save_later()
		isos = self._try('iso', c.supported_isos)
		if isos:
			upd['isoOptions'] = isos
		shutters = self._try('shutter', c.supported_shutters) or {}
		upd['wbRange'] = self._try('wb', c.white_balance_range)
		upd['tintRange'] = self._try('tint', c.tint_range)
		supported = self._try('fps', c.get, '/system/supportedFormats') or {}
		self._supported = supported.get('supportedFormats', [])
		upd['resolutionOptions'] = [{
			'value': res_key(f.get('recordResolution')),
			'label': '{} · {}×{}'.format((f.get('resolutionDescriptor') or {}).get('description') or '',
										 f['recordResolution']['width'], f['recordResolution']['height']).strip(' ·'),
		} for f in self._supported if f.get('recordResolution')]
		upd['colorspaceOptions'] = self._try('colorspace', c.supported_dynamic_ranges) or []
		lenses = self._try('lenscams', c.lens_cameras)
		self._lenses = [l for l in (lenses or []) if l.get('isAvailable', True) and l.get('id')]
		zd = self._try('lens', c.zoom_description) or {}
		fl = zd.get('focalLength') or {}
		if self._lenses:
			# The phone's own lens list: switching selects the physical lens.
			back = sorted((l for l in self._lenses if l.get('facing') != 'front'), key=lambda l: l.get('focalLength', 0))
			front = [l for l in self._lenses if l.get('facing') == 'front']
			upd['lensOptions'] = [{'value': l['id'], 'mm': l.get('focalLength'), 'zoom': (l.get('zoomFactor') or '').replace('x', '×'),
								   'facing': l.get('facing', 'back')} for l in back + front]
			upd['lensNative'] = True
			upd['lensMain'] = next((l.get('focalLength') for l in back if l.get('zoomFactor') == '1x'), None)
			if fl.get('min') is not None:
				upd['lensRange'] = [fl['min'], fl.get('max')]
		elif fl.get('min') is not None and fl.get('max') is not None:
			lo, hi = fl['min'], fl['max']
			upd['lensRange'] = [lo, hi]
			native = NATIVE_LENSES_MM.get(upd['product'].strip())
			if native:
				opts = [mm for mm in native if lo <= mm <= hi]
			else:
				opts = sorted({lo, hi} | {mm for mm in LENS_PRESETS_MM if lo <= mm <= hi})
			upd['lensOptions'] = opts
			upd['lensNative'] = bool(native)
			# 1x reference for the zoom factor shown next to each focal length
			upd['lensMain'] = min(opts, key=lambda mm: abs(mm - 25)) if opts else None
		self._update(**upd)
		self._shutter_options = shutters
		self._caps_dirty = False

	def _poll_settings(self):
		c = self.client
		upd = {}
		v = self._try('iso', c.iso)
		if v is not None:
			upd['iso'] = v
		sh = self._try('shutter', c.shutter)
		if sh:
			upd['shutter'] = format_shutter(sh)
			mode = 'ShutterAngle' if sh.get('shutterSpeed') is None and sh.get('shutterAngle') is not None else 'ShutterSpeed'
			upd['shutterMode'] = mode
			if mode == 'ShutterAngle':
				self._angle_scale = 100 if sh['shutterAngle'] > 360 else 1
			opts = self._shutter_options
			if mode == 'ShutterAngle':
				upd['shutterOptions'] = [format_shutter({'shutterAngle': a}) for a in opts.get('shutterAngles', [])]
			else:
				upd['shutterOptions'] = ['1/{}'.format(s) for s in opts.get('shutterSpeeds', [])]
		v = self._try('wb', c.white_balance)
		if v is not None:
			upd['wb'] = v
		v = self._try('tint', c.tint)
		if v is not None:
			upd['tint'] = v
		fmt = self._try('fps', c.format)
		if fmt:
			fr = fmt.get('frameRate')
			if self._device_format and fr != self._device_format.get('frameRate'):
				self._caps_dirty = True  # shutter/ISO lists can depend on frame rate
			self._device_format = fmt
			dr = self._try('colorspace', c.dynamic_range)
			if dr is not None:
				self._device_dr = dr
			upd.update(self._format_state())
		z = self._try('lens', c.zoom)
		if z:
			upd['lens'] = z.get('focalLength')
		if self._lenses:
			active = self._try('lenscams', c.get, '/lens/cameras/active')
			if active:
				upd['lensId'] = active.get('id')
				if active.get('facing') == 'front':
					upd['lens'] = 'Front'
		sl = self._try('slate', c.slate)
		if sl:
			upd['letter'] = (sl.get('project') or {}).get('camera')
			upd['reel'] = (sl.get('clip') or {}).get('reel')
			upd['clipName'] = (sl.get('clip') or {}).get('clipName')
		st = self._try('stream', c.stream_status)
		if st:
			status, bitrate = st.get('status'), st.get('bitrate') or 0
			upd['streaming'] = status
			upd['streamBitrate'] = bitrate
			if status not in (None, 'Idle'):
				plat = self._try('stream', c.active_platform) or {}
				upd['liveOurs'] = plat.get('platform') == livestream.PLATFORM_ID
			else:
				upd['liveOurs'] = False
			upd['liveWarning'] = self._stream_warning(status, bitrate, st.get('effectiveVideoFormat'))
		self._update(**upd)

	# --- recording format -------------------------------------------------------

	def _format_state(self):
		"""UI fields for the real recording format (the saved one while live)."""
		shown = self.saved_format if self.live_mode else self._device_format
		if not shown:
			return {}
		entry = self._entry(res_key(shown.get('recordResolution')))
		return {
			'fps': shown.get('frameRate'),
			'colorspace': shown.get('_dynamicRange') if self.live_mode else self._device_dr,
			'codec': shown.get('codec'),
			'resolution': res_key(shown.get('recordResolution')),
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

	def _build_format(self, base, codec=None, resolution=None, fps=None):
		"""Full /system/format object (the phone rejects partial ones)."""
		fmt = dict(base)
		entry = self._entry(resolution or res_key(base.get('recordResolution')))
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
		self.manager._log('{}: live view ended, recording format restored'.format(self.state['name'] or self.state['address']))

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

	def queue_set(self, param, value):
		self.commands.put(('set', param, value))

	def queue(self, *cmd):
		self.commands.put(cmd)

	def _execute(self, kind, *args):
		c = self.client
		label = kind
		try:
			if kind == 'set':
				param, value = args
				label = 'set {}'.format(param)
				self._set(param, value)
				self._next_full = 0  # read back promptly
			elif kind == 'autowb':
				label = 'auto WB'
				c.auto_white_balance()
				self._next_full = 0
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
				self._next_full = 0
			elif kind == 'refresh':
				self._reconnect()
		except api.ApiError as e:
			self._error('{} failed ({})'.format(label, e))
		except (ValueError, TypeError) as e:
			self._error('{}: bad value ({})'.format(label, e))

	def _set(self, param, value):
		c = self.client
		if param == 'iso':
			c.set_iso(int(float(value)))
			self._update(iso=int(float(value)))
		elif param == 'shutter':
			parsed = parse_shutter(value, self.state['shutterMode'])
			angle = parsed.get('angle')
			if angle is not None:
				angle *= self._angle_scale
			c.set_shutter(speed=parsed.get('speed'), angle=angle)
		elif param == 'wb':
			c.set_white_balance(int(float(value)))
			self._update(wb=int(float(value)))
		elif param == 'tint':
			c.set_tint(int(float(value)))
			self._update(tint=int(float(value)))
		elif param in ('fps', 'codec', 'resolution', 'colorspace'):
			self._set_format_param(param, value)
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

	def _set_lens(self, value):
		"""A lens id from the phone's list, or a focal length in mm: picks the
		back lens with that focal length, else zooms (phones without /lens/cameras)."""
		c = self.client
		value = str(value).strip()
		lens = next((l for l in self._lenses if l['id'] == value), None)
		if lens is None and value.lower() == 'front':
			lens = next((l for l in self._lenses if l.get('facing') == 'front'), None)
		if lens is None and self._lenses:
			mm = float(value.lower().replace('mm', ''))
			lens = next((l for l in self._lenses if l.get('facing') != 'front' and l.get('focalLength') == mm), None)
		if lens is not None:
			c.set_active_lens(lens['id'])
			self._update(lensId=lens['id'], lens='Front' if lens.get('facing') == 'front' else lens.get('focalLength'))
		else:
			mm = int(float(value.lower().replace('mm', '')))
			c.set_focal_length(mm)
			self._update(lens=mm)

	def do_record(self, recording, go, skip_reason=None):
		"""Runs on a short-lived thread; `go` is a shared Event so every camera
		fires as close to simultaneously as possible."""
		self._update(recordPending=True)
		go.wait(2.0)
		if skip_reason:
			self._error('REC skipped: ' + skip_reason)
			self._update(recordPending=False)
			return
		try:
			self.client.record(recording)
			self._update(recording=bool(recording))
		except api.ApiError as e:
			self._error('{} failed ({})'.format('REC' if recording else 'STOP', e))
		finally:
			self._update(recordPending=False)
			self._next_fast = 0
			self.commands.put(('wake',))


class CameraManager:
	def __init__(self, config_path=None, web_dir=WEB_DIR, autosave=True):
		self.lock = threading.RLock()
		self.config_path = config_path
		self.web_dir = web_dir
		self.autosave = autosave
		self.workers = {}          # id -> CameraWorker, in insertion order
		self.global_values = {}
		self.log = []
		self.discovery = {'running': False, 'mode': None, 'results': [], 'error': None, 'finished': 0}
		self._save_timer = None
		self.live = livestream.LiveServer()
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
		for cam in cfg.get('cameras', []):
			try:
				self._spawn(cam.get('id') or uuid.uuid4().hex[:8], cam['address'],
							cam.get('name', ''), cam.get('active', True),
							cam.get('liveMode', False), cam.get('savedFormat'))
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
							 **({'liveMode': True, 'savedFormat': w.saved_format} if w.live_mode else {})}
							for w in self.workers.values()],
				'global': self.global_values,
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
		with self.lock:
			self.log.append({'t': time.time(), 'msg': msg})
			del self.log[:-50]

	# --- cameras ------------------------------------------------------------------

	def _spawn(self, cam_id, address, name, active, live_mode=False, saved_format=None):
		w = CameraWorker(self, cam_id, address, name, active, live_mode, saved_format)
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

	def move_camera(self, cam_id, delta):
		with self.lock:
			ids = list(self.workers)
			i = ids.index(cam_id)
			j = max(0, min(len(ids) - 1, i + int(delta)))
			ids.insert(j, ids.pop(i))
			self.workers = {k: self.workers[k] for k in ids}
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
		self._worker(cam_id).queue_set(param, value)

	def set_global(self, param, value):
		"""Push one value to every active camera, overriding individual values."""
		if param not in PARAMS:
			raise ValueError('unknown parameter ' + str(param))
		with self.lock:
			self.global_values[param] = value
		for cam_id in self.active_ids():
			self.set_param(cam_id, param, value)
		self._save_later()

	def auto_wb(self, cam_ids):
		for cam_id in cam_ids:
			self._worker(cam_id).queue('autowb')

	def stream(self, cam_ids, enabled):
		for cam_id in cam_ids:
			self._worker(cam_id).queue('stream', bool(enabled))

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
				if mode == 'scan':
					results = discovery.discover_subnet()
				else:
					results = discovery.discover_bonjour()
				err = None
			except Exception as e:
				results, err = [], str(e)
			with self.lock:
				self.discovery.update(running=False, results=results, error=err, finished=time.time())

		threading.Thread(target=job, name='lucam-discovery', daemon=True).start()

	# --- state ----------------------------------------------------------------------

	def snapshot(self):
		with self.lock:
			workers = list(self.workers.values())
			disc = dict(self.discovery)
			addresses = {w.state['address'] for w in workers}
			disc['results'] = [dict(r, added=r['address'] in addresses) for r in disc['results']]
			glob = dict(self.global_values)
			log = list(self.log[-10:])
		cams = [w.snapshot() for w in workers]
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

	# --- HTTP API -------------------------------------------------------------------

	def handle_http(self, method, path, body=b''):
		"""Request handler used by server.py. Returns (status, content_type, bytes)."""
		path = path.split('?', 1)[0]
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
		if method != 'POST':
			raise KeyError('no route')
		if parts == ['cameras', 'add']:
			return {'id': self.add_camera(p['address'], p.get('name', ''))}
		if parts == ['discover']:
			self.discover(p.get('mode', 'bonjour'))
			return None
		if parts == ['global', 'set']:
			self.set_global(p['param'], p['value'])
			return None
		if parts == ['global', 'record']:
			self.record_all(bool(p['recording']))
			return None
		if parts == ['global', 'autowb']:
			self.auto_wb(self.active_ids())
			return None
		if parts == ['global', 'stream']:
			self.stream(self.active_ids(), p['enabled'])
			return None
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
				self.set_param(cam_id, p['param'], p['value'])
			elif action == 'record':
				self.record([cam_id], bool(p['recording']))
			elif action == 'autowb':
				self.auto_wb([cam_id])
			elif action == 'stream':
				self.stream([cam_id], p['enabled'])
			elif action == 'refresh':
				self.refresh(cam_id)
			elif action == 'move':
				self.move_camera(cam_id, p.get('delta', 0))
			else:
				raise KeyError('no route')
			return None
		raise KeyError('no route')
