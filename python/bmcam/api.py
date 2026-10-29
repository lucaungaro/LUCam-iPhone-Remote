"""Thin client for the Blackmagic Camera REST API (/control/api/v1).

Endpoints follow Blackmagic's "REST API for Blackmagic Cameras" developer
document (Aug 2025). The Blackmagic Camera app for iOS exposes the same API
since version 3.4. Only the standard library is used.
"""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = '/control/api/v1'

_UNVERIFIED_SSL = ssl.create_default_context()
_UNVERIFIED_SSL.check_hostname = False
_UNVERIFIED_SSL.verify_mode = ssl.CERT_NONE


class ApiError(Exception):
	def __init__(self, status, message):
		super().__init__('HTTP {}: {}'.format(status, message) if status else message)
		self.status = status


def parse_address(address):
	"""Split '192.168.1.20', 'cam-a.local:8080' or 'https://cam.local' into
	(scheme, host, port)."""
	address = address.strip().rstrip('/')
	scheme = 'http'
	if '://' in address:
		scheme, address = address.split('://', 1)
		scheme = scheme.lower()
	address = address.split('/', 1)[0]
	port = 443 if scheme == 'https' else 80
	if address.startswith('['):  # [ipv6]:port
		host, _, rest = address[1:].partition(']')
		if rest.startswith(':'):
			port = int(rest[1:])
	elif address.count(':') == 1:
		host, p = address.split(':')
		port = int(p)
	else:
		host = address
	if not host:
		raise ValueError('Empty camera address')
	return scheme, host, port


def format_address(scheme, host, port):
	default = 443 if scheme == 'https' else 80
	h = '[{}]'.format(host) if ':' in host else host
	addr = h if port == default else '{}:{}'.format(h, port)
	return addr if scheme == 'http' else '{}://{}'.format(scheme, addr)


class CameraClient:
	"""Blocking HTTP client for one camera. Call from worker threads only."""

	def __init__(self, address, timeout=2.0):
		self.scheme, self.host, self.port = parse_address(address)
		self.timeout = timeout

	@property
	def address(self):
		return format_address(self.scheme, self.host, self.port)

	@property
	def base_url(self):
		h = '[{}]'.format(self.host) if ':' in self.host else self.host
		return '{}://{}:{}{}'.format(self.scheme, h, self.port, API_ROOT)

	def request(self, method, path, body=None, timeout=None, raw=None, content_type=None):
		data = None
		headers = {'Accept': 'application/json'}
		if raw is not None:
			data = raw if isinstance(raw, bytes) else raw.encode('utf-8')
			headers['Content-Type'] = content_type or 'application/octet-stream'
		elif body is not None:
			data = json.dumps(body).encode('utf-8')
			headers['Content-Type'] = 'application/json'
		req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
		ctx = _UNVERIFIED_SSL if self.scheme == 'https' else None
		try:
			with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=ctx) as resp:
				raw = resp.read()
		except urllib.error.HTTPError as e:
			detail = ''
			try:
				detail = e.read().decode('utf-8', 'replace')[:200]
			except Exception:
				pass
			raise ApiError(e.code, detail or e.reason)
		except (urllib.error.URLError, OSError) as e:
			raise ApiError(0, str(getattr(e, 'reason', e)))
		if not raw:
			return None
		try:
			return json.loads(raw.decode('utf-8'))
		except ValueError:
			return None

	def get(self, path, timeout=None):
		return self.request('GET', path, timeout=timeout)

	def put(self, path, body=None):
		return self.request('PUT', path, body)

	def post(self, path, body=None):
		return self.request('POST', path, body)

	# --- system -------------------------------------------------------------

	def product(self, timeout=None):
		return self.get('/system/product', timeout=timeout) or {}

	def format(self):
		return self.get('/system/format') or {}

	def frame_rates(self, fmt=None):
		"""Frame rates available for the current codec and resolution."""
		fmt = fmt or self.format()
		formats = (self.get('/system/supportedFormats') or {}).get('supportedFormats', [])
		res = fmt.get('recordResolution') or {}
		codec = fmt.get('codec')
		rates = []
		for f in formats:
			r = f.get('recordResolution') or {}
			if res and (r.get('width'), r.get('height')) != (res.get('width'), res.get('height')):
				continue
			if codec and f.get('codecs') and codec not in f['codecs']:
				continue
			for fr in f.get('frameRates', []):
				if fr not in rates:
					rates.append(fr)
		return rates

	def set_frame_rate(self, rate):
		self.put('/system/format', {'frameRate': str(rate)})

	def dynamic_range(self):
		"""Colour space as the app names it: 'Video', 'Extended Video', 'Film', 'HLG'."""
		return (self.get('/system/dynamicRange') or {}).get('dynamicRange')

	def supported_dynamic_ranges(self):
		return (self.get('/system/supportedDynamicRanges') or {}).get('supportedDynamicRanges', [])

	def set_dynamic_range(self, value):
		self.put('/system/dynamicRange', {'dynamicRange': str(value)})

	# --- transport ----------------------------------------------------------

	def is_recording(self):
		return bool((self.get('/transports/0/record') or {}).get('recording'))

	def record(self, recording):
		# POST /record and /stop are current; PUT /record is the deprecated
		# form that older firmware may still need.
		try:
			self.post('/transports/0/record' if recording else '/transports/0/stop')
		except ApiError as e:
			if e.status not in (404, 405, 501):
				raise
			self.put('/transports/0/record', {'recording': bool(recording)})

	def timecode(self):
		return (self.get('/transports/0/timecode') or {}).get('display')

	# --- video --------------------------------------------------------------

	def iso(self):
		return (self.get('/video/iso') or {}).get('iso')

	def supported_isos(self):
		return (self.get('/video/supportedISOs') or {}).get('supportedISOs', [])

	def set_iso(self, iso):
		self.put('/video/iso', {'iso': int(iso)})

	def shutter(self):
		return self.get('/video/shutter') or {}

	def supported_shutters(self):
		return self.get('/video/supportedShutters') or {}

	def set_shutter(self, speed=None, angle=None):
		if angle is not None:
			self.put('/video/shutter', {'shutterAngle': float(angle)})
		else:
			self.put('/video/shutter', {'shutterSpeed': int(speed)})

	def white_balance(self):
		return (self.get('/video/whiteBalance') or {}).get('whiteBalance')

	def white_balance_range(self):
		d = (self.get('/video/whiteBalance/description') or {}).get('whiteBalance') or {}
		return [d.get('min'), d.get('max')] if d else None

	def set_white_balance(self, kelvin):
		self.put('/video/whiteBalance', {'whiteBalance': int(kelvin)})

	def auto_white_balance(self):
		self.put('/video/whiteBalance/doAuto')

	def tint(self):
		return (self.get('/video/whiteBalanceTint') or {}).get('whiteBalanceTint')

	def tint_range(self):
		d = (self.get('/video/whiteBalanceTint/description') or {}).get('whiteBalanceTint') or {}
		return [d.get('min'), d.get('max')] if d else None

	def set_tint(self, tint):
		self.put('/video/whiteBalanceTint', {'whiteBalanceTint': int(tint)})

	# --- lens ---------------------------------------------------------------

	def zoom(self):
		return self.get('/lens/zoom') or {}

	def zoom_description(self):
		return self.get('/lens/zoom/description') or {}

	def set_focal_length(self, mm):
		self.put('/lens/zoom', {'focalLength': int(mm)})

	def lens_cameras(self):
		"""Physical lenses (iPhone app): [{'id': 'Lens24mm', 'focalLength': 24,
		'zoomFactor': '1x', 'facing': 'back', 'isAvailable': True, 'isActive': ...}]"""
		return (self.get('/lens/cameras') or {}).get('cameras', [])

	def set_active_lens(self, lens_id):
		self.put('/lens/cameras/active', {'id': str(lens_id)})

	# --- slate --------------------------------------------------------------

	def slate(self):
		return self.get('/slates/nextClip') or {}

	def set_camera_letter(self, letter):
		self.put('/slates/nextClip', {'project': {'camera': str(letter)}})

	def set_reel(self, reel):
		self.put('/slates/nextClip', {'clip': {'reel': int(reel)}})

	# --- livestream ---------------------------------------------------------

	def stream_status(self):
		"""{'status': 'Idle'|'Connecting'|'Streaming'|..., 'bitrate': bps, ...}"""
		return self.get('/livestreams/0') or {}

	def stream(self, enabled):
		self.put('/livestreams/0/start' if enabled else '/livestreams/0/stop')

	def active_platform(self):
		return self.get('/livestreams/0/activePlatform') or {}

	def set_active_platform(self, platform, server, key, quality):
		self.put('/livestreams/0/activePlatform',
				 {'platform': platform, 'server': server, 'key': key, 'quality': quality})

	def platform(self, name):
		return self.get('/livestreams/platforms/' + urllib.parse.quote(name)) or {}

	def upload_platform(self, filename, xml):
		self.request('PUT', '/livestreams/customPlatforms/' + urllib.parse.quote(filename),
					 raw=xml, content_type='application/xml')


# The Blackmagic Camera app serves the API over HTTPS on port 4444 (self-signed
# certificate); Blackmagic's hardware cameras use plain HTTP on port 80.
APP_PORT = 4444


def address_candidates(address):
	"""Addresses to try for what the user typed. An explicit scheme is used as
	is; otherwise the iPhone app's HTTPS port comes first."""
	address = address.strip()
	if '://' in address:
		return [address]
	_, host, port = parse_address(address)
	if address.rsplit(']', 1)[-1].count(':') == 1:  # host:port or [v6]:port
		return [format_address('https', host, port), format_address('http', host, port)]
	return [format_address('https', host, APP_PORT), format_address('http', host, 80), format_address('https', host, 443)]


def resolve(address, timeout=1.5):
	"""Return (working_address, product_info) for the first candidate that
	answers the camera API, or (None, None)."""
	for candidate in address_candidates(address):
		info = probe(candidate, timeout=timeout)
		if info is not None:
			return candidate, info
	return None, None


def probe(address, timeout=1.0):
	"""Return product info if `address` answers the camera REST API, else None."""
	client = CameraClient(address, timeout=timeout)
	for path in ('/system/product', '/transports/0'):
		try:
			info = client.get(path)
		except ApiError:
			continue
		if isinstance(info, dict):
			return info if path == '/system/product' else {'productName': 'Blackmagic camera'}
	return None
