#!/usr/bin/env python3
"""Simulated Blackmagic camera(s) speaking the REST API and the event WebSocket,
for testing without iPhones. Each instance listens on its own port and can
register itself with Bonjour (macOS `dns-sd`) so discovery can be tested too.

    python3 tools/mock_camera.py --count 3 --base-port 8081 --bonjour

Like the iPhone app, a mock pushes every change of state to the clients
subscribed at /control/api/v1/event/websocket: the timecode once a second, and
on every frame while recording. `--no-events` makes a camera without that
channel.

A mock also counts what it is sent, which is what tests/test_engine.py checks
the controller against, and it can be told to misbehave. These paths are the
mock's own, not part of the camera API:

    GET  /mock/stats                  requests received: in all, with no listener connected,
                                      while recording; listeners connected
    POST /mock/set     {"wb": 6000}   change a value as if on the phone (wb, tint, iso, focus,
                                      autoFocus, deviceLUT)
    POST /mock/stop                   stop recording as the camera would by itself
    POST /mock/delay   {"ms": 300}    answer every request that much later
    POST /mock/deaf    {"on": true}   acknowledge REC and STOP without carrying them out
    POST /mock/drop                   close every event channel
"""

import argparse
import base64
import hashlib
import json
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = '/control/api/v1'
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
# The lens list an iPhone 17 Pro Max reports (two front lenses, '77mm' id at 100 mm).
LENSES = [
	{'id': 'Lens13mm', 'focalLength': 13, 'zoomFactor': '0.5x', 'facing': 'back'},
	{'id': 'Lens24mm', 'focalLength': 24, 'zoomFactor': '1x', 'facing': 'back'},
	{'id': 'LensWASecondary', 'focalLength': 48, 'zoomFactor': '2x', 'facing': 'back'},
	{'id': 'Lens77mm', 'focalLength': 100, 'zoomFactor': '4x', 'facing': 'back'},
	{'id': 'Lens200mm', 'focalLength': 200, 'zoomFactor': '8x', 'facing': 'back'},
	{'id': 'LensFrontUltraWide', 'focalLength': 19, 'zoomFactor': '0.5x', 'facing': 'front'},
	{'id': 'LensFront', 'focalLength': 19, 'zoomFactor': '0.5x', 'facing': 'front'},
]
CODECS = ['Apple ProRes 422 HQ:Default', 'Apple ProRes 422:Default', 'HEVC (H.265):High', 'H.264:High', 'H.264:Medium']
FRAME_RATE = 24   # timecode events a second while recording


def make_state(name, iris_controllable=True):
	return {
		'activeLens': 'Lens24mm',
		'iris': 2.8 if iris_controllable else 1.78,
		'irisControllable': iris_controllable,
		'product': {'deviceName': name, 'productName': 'Blackmagic Camera (iPhone, simulated)', 'softwareVersion': '3.5.3'},
		'recording': False,
		'presets': {},
		'recStart': 0,
		'iso': 400,
		'autoExposure': 'Off',
		'autoLens': False,
		'supportedISOs': [50, 100, 200, 400, 800, 1250, 1600, 2000, 3200],
		'shutterSpeed': 50,
		'wb': 5600, 'tint': 0,
		'focus': 0.5, 'autoFocus': False,
		'dynamicRange': 'Film',
		'format': {'codec': 'Apple ProRes 422 HQ:Default', 'frameRate': '25',
				   'recordResolution': {'width': 3840, 'height': 2160}, 'sensorResolution': {'width': 3840, 'height': 2160},
				   'resolutionDescriptor': {'group': '4K', 'aspectRatio': '16:9', 'description': '4K'},
				   'offSpeedEnabled': False},
		'zoom': 24,
		'slate': {'clip': {'clipName': 'A001_C001', 'reel': 1, 'scene': '1', 'take': 1},
				  'project': {'projectName': 'Mock', 'camera': 'A'}},
		'stream': 'Idle',
		# like the iPhone app: the screen has a LUT switch, HDMI has LUT and clean feed
		'monitor': {('Device', 'displayLUT'): False, ('HDMI', 'displayLUT'): False, ('HDMI', 'cleanFeed'): False},
	}


def timecode():
	"""Time of day, like a phone following a timecode unit."""
	t = time.time()
	lt = time.localtime(t)
	return '{:02d}:{:02d}:{:02d}:{:02d}'.format(lt.tm_hour, lt.tm_min, lt.tm_sec, int((t % 1) * FRAME_RATE))


class Listener:
	"""One client of the event channel."""

	def __init__(self, wfile):
		self.wfile = wfile
		self.lock = threading.Lock()
		self.properties = set()
		self.alive = True

	def send(self, opcode, payload=b''):
		n = len(payload)
		head = bytearray([0x80 | opcode])
		if n < 126:
			head.append(n)
		elif n < 65536:
			head.append(126)
			head += struct.pack('>H', n)
		else:
			head.append(127)
			head += struct.pack('>Q', n)
		try:
			with self.lock:
				self.wfile.write(bytes(head) + payload)
				self.wfile.flush()
		except (OSError, ValueError):
			self.alive = False

	def event(self, prop, value):
		self.send(0x1, json.dumps({'type': 'event', 'data': {'action': 'propertyValueChanged', 'property': prop, 'value': value}}).encode())


class MockCamera:
	def __init__(self, name, iris_controllable=True, events=True):
		self.state = make_state(name, iris_controllable)
		self.lock = threading.RLock()
		self.events = events
		self.ws_broken = False   # lists its events, but the WebSocket cannot be opened
		self.listeners = []
		self.delay = 0.0
		self.deaf = False
		self.stats = {'requests': 0, 'withoutListener': 0, 'whileRecording': 0, 'gets': 0, 'writes': 0}
		self.last = []   # the latest requests: [time, method, path]
		threading.Thread(target=self._clock, daemon=True).start()

	# --- what a GET returns, which is also what an event carries -----------------

	def read(self, p):
		s = self.state
		if p == '/transports/0/timecode':
			return {'display': timecode(), 'timeline': timecode()}
		routes = {
			'/system/product': s['product'],
			'/system/format': s['format'],
			'/system/supportedFormats': {'supportedFormats': [{
				'codecs': CODECS,
				'frameRates': ['23.98', '24', '25', '29.97', '30', '48', '50', '59.94', '60'],
				'recordResolution': {'width': w, 'height': h}, 'sensorResolution': {'width': w, 'height': h},
				'resolutionDescriptor': {'group': d, 'aspectRatio': '16:9', 'description': d}}
				for w, h, d in ((3840, 2160, '4K'), (1920, 1080, 'HD'))]},
			'/system/dynamicRange': {'dynamicRange': s['dynamicRange']},
			'/system/supportedDynamicRanges': {'supportedDynamicRanges': ['Video', 'Extended Video', 'Film', 'HLG']},
			'/transports/0': {'mode': 'InputRecord' if s['recording'] else 'InputPreview'},
			'/transports/0/record': {'recording': s['recording']},
			'/video/iso': {'iso': s['iso']},
			'/video/autoExposure': {'mode': s['autoExposure']},
			'/lens/cameras/auto': {'supported': True, 'enabled': s['autoLens']},
			'/video/supportedISOs': {'supportedISOs': s['supportedISOs']},
			'/video/shutter': {'continuousShutterAutoExposure': False, 'shutterSpeed': s['shutterSpeed']},
			'/video/supportedShutters': {'shutterSpeeds': [24, 25, 30, 48, 50, 60, 100, 120, 200, 250, 500, 1000]},
			'/video/whiteBalance': {'whiteBalance': s['wb']},
			'/video/whiteBalance/description': {'whiteBalance': {'min': 2500, 'max': 10000}},
			'/video/whiteBalanceTint': {'whiteBalanceTint': s['tint']},
			'/video/whiteBalanceTint/description': {'whiteBalanceTint': {'min': -50, 'max': 50}},
			'/lens/zoom': {'focalLength': s['zoom'], 'normalised': 0.1},
			'/lens/focus': {'normalised': s['focus']},
			'/lens/focus/description': {'controllable': True},
			'/lens/focus/autoFocus': {'supported': True, 'enabled': s['autoFocus'],
									  'mode': 'Continuous' if s['autoFocus'] else 'OneShot'},
			'/lens/cameras': {'cameras': [dict(l, index=i, isAvailable=True, isActive=l['id'] == s['activeLens'])
										 for i, l in enumerate(LENSES)]},
			'/lens/cameras/active': dict(next(l for l in LENSES if l['id'] == s['activeLens']), isActive=True, isAvailable=True),
			'/lens/iris': {'apertureStop': s['iris'],
						   'continuousApertureAutoExposure': s['autoExposure'] != 'Off' and s['irisControllable']},
			'/lens/iris/description': {'controllable': s['irisControllable'],
									   'apertureStop': {'min': 1.6, 'max': 8.0} if s['irisControllable'] else {'min': 1.78, 'max': 1.78}},
			'/lens/zoom/description': {'controllable': True, 'focalLength': {'adjustable': True, 'min': 13, 'max': 120}},
			'/slates/nextClip': s['slate'],
			'/monitoring/display': {'displays': ['Device']},
			'/monitoring/Device/displayLUT': {'enabled': s['monitor'][('Device', 'displayLUT')]},
			'/monitoring/HDMI/displayLUT': {'enabled': s['monitor'][('HDMI', 'displayLUT')]},
			'/monitoring/HDMI/cleanFeed': {'enabled': s['monitor'][('HDMI', 'cleanFeed')]},
			'/livestreams/0': {'status': s['stream'], 'bitrate': 0, 'effectiveVideoFormat': '1080p25'},
			'/presets': {'presets': sorted(s['presets'])},
			'/presets/active': {'preset': ''},
		}
		return routes.get(p)

	# Like the app: the phone's screen is /monitoring/Device/... over REST and
	# /monitoring/LCD/... on the event channel; autofocus and the screen's LUT have no event.
	NO_EVENT = ('/lens/focus/autoFocus', '/monitoring/Device/displayLUT', '/monitoring/display', '/transports/0')
	EVENT_NAMES = {'/monitoring/Device/displayLUT': '/monitoring/LCD/displayLUT'}

	def event_list(self):
		with self.lock:
			paths = ['/transports/0/timecode'] + [p for p in self._all_paths() if p not in self.NO_EVENT]
		return paths + ['/monitoring/LCD/displayLUT', '/audio/channel/0/level']

	def _all_paths(self):
		return ['/system/product', '/system/format', '/system/supportedFormats', '/system/dynamicRange',
				'/system/supportedDynamicRanges', '/transports/0', '/transports/0/record', '/video/iso',
				'/video/autoExposure', '/lens/cameras/auto', '/video/supportedISOs', '/video/shutter',
				'/video/supportedShutters', '/video/whiteBalance', '/video/whiteBalance/description',
				'/video/whiteBalanceTint', '/video/whiteBalanceTint/description', '/lens/zoom', '/lens/focus',
				'/lens/focus/description', '/lens/focus/autoFocus', '/lens/cameras', '/lens/cameras/active',
				'/lens/iris', '/lens/iris/description', '/lens/zoom/description', '/slates/nextClip',
				'/monitoring/display', '/monitoring/Device/displayLUT', '/monitoring/HDMI/displayLUT',
				'/monitoring/HDMI/cleanFeed', '/livestreams/0', '/presets']

	def snapshot(self):
		return {p: json.dumps(self.read(p), sort_keys=True) for p in self._all_paths() if p not in self.NO_EVENT}

	def publish(self, before):
		"""Push an event for every value that differs from the snapshot `before`."""
		with self.lock:
			changed = [(p, self.read(p)) for p, old in before.items() if json.dumps(self.read(p), sort_keys=True) != old]
			listeners = [l for l in self.listeners if l.alive]
		for p, value in changed:
			for l in listeners:
				if p in l.properties:
					l.event(p, value)

	def _clock(self):
		"""Timecode events: once a second, on every frame while recording."""
		while True:
			with self.lock:
				recording = self.state['recording']
				listeners = [l for l in self.listeners if l.alive and '/transports/0/timecode' in l.properties]
			value = {'display': timecode(), 'timeline': timecode()}
			for l in listeners:
				l.event('/transports/0/timecode', value)
			time.sleep(1.0 / FRAME_RATE if recording else 1.0)

	def count(self, method, path):
		with self.lock:
			st = self.stats
			st['requests'] += 1
			st['gets' if method == 'GET' else 'writes'] += 1
			if not any(l.alive for l in self.listeners):
				st['withoutListener'] += 1
			if self.state['recording'] and path not in ('/transports/0/record', '/transports/0/stop'):
				st['whileRecording'] += 1
			self.last.append([round(time.time(), 3), method, path])
			del self.last[:-200]

	def set_recording(self, rec):
		s = self.state
		if rec and not s['recording']:
			s['recStart'] = time.time()
			c = s['slate']['clip']
			c['clipName'] = '{}{:03d}_C{:03d}'.format(s['slate']['project']['camera'], c['reel'], c['take'])
		if not rec and s['recording']:
			s['slate']['clip']['take'] += 1
		s['recording'] = rec


def make_handler(cam, verbose):
	state, lock = cam.state, cam.lock

	class Handler(BaseHTTPRequestHandler):
		protocol_version = 'HTTP/1.1'

		def log_message(self, fmt, *args):
			if verbose:
				super().log_message(fmt, *args)

		def _send(self, code, payload=None):
			body = json.dumps(payload).encode() if payload is not None else b''
			self.send_response(code)
			if body:
				self.send_header('Content-Type', 'application/json')
			self.send_header('Content-Length', str(len(body)))
			self.send_header('Connection', 'close')   # like the app: one request per connection
			self.end_headers()
			self.wfile.write(body)
			self.close_connection = True

		def _body(self):
			n = int(self.headers.get('Content-Length') or 0)
			return json.loads(self.rfile.read(n)) if n else {}

		def _path(self):
			if not self.path.startswith(ROOT):
				return None
			return self.path[len(ROOT):].split('?')[0]

		def _arrive(self, method, p):
			cam.count(method, p)
			if cam.delay:
				time.sleep(cam.delay)

		# --- the mock's own controls ----------------------------------------------

		def _mock(self, method):
			what = self.path.split('?')[0][len('/mock/'):]
			body = self._body() if method == 'POST' else {}
			if method == 'GET' and what == 'stats':
				with lock:
					return self._send(200, dict(cam.stats, listeners=sum(1 for l in cam.listeners if l.alive),
											   recording=state['recording'], last=cam.last[-50:]))
			if method != 'POST':
				return self._send(404, {'error': 'not found'})
			with lock:
				before = cam.snapshot()
				if what == 'set':
					for k, v in body.items():
						if k == 'deviceLUT':
							state['monitor'][('Device', 'displayLUT')] = bool(v)
						elif k in ('wb', 'tint', 'iso', 'focus', 'autoFocus'):
							state[k] = v
				elif what == 'stop':
					cam.set_recording(False)
				elif what == 'delay':
					cam.delay = float(body.get('ms', 0)) / 1000.0
				elif what == 'deaf':
					cam.deaf = bool(body.get('on', True))
				elif what == 'drop':
					for l in cam.listeners:
						l.send(0x8)
						l.alive = False
				else:
					return self._send(404, {'error': 'not found'})
			cam.publish(before)
			self._send(204)

		# --- the event channel ------------------------------------------------------

		def _websocket(self):
			key = self.headers.get('Sec-WebSocket-Key', '')
			accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
			# counted as listening before the answer goes out, so a request that follows
			# the answer at once is never taken for one with no listener
			me = Listener(self.wfile)
			with lock:
				cam.listeners.append(me)
			self.send_response(101, 'Switching Protocols')
			self.send_header('Upgrade', 'websocket')
			self.send_header('Connection', 'Upgrade')
			self.send_header('Sec-WebSocket-Accept', accept)
			self.end_headers()
			self.wfile.flush()
			self.close_connection = True
			try:
				while me.alive:
					head = self.rfile.read(2)
					if len(head) < 2:
						break
					opcode, n = head[0] & 0x0F, head[1] & 0x7F
					if n == 126:
						n = struct.unpack('>H', self.rfile.read(2))[0]
					elif n == 127:
						n = struct.unpack('>Q', self.rfile.read(8))[0]
					mask = self.rfile.read(4) if head[1] & 0x80 else None
					data = self.rfile.read(n)
					if mask:
						data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
					if opcode == 0x8:
						me.send(0x8)
						break
					if opcode == 0x9:
						me.send(0xA, data)
					elif opcode == 0x1:
						self._ws_request(me, data)
			except (OSError, ValueError):
				pass
			finally:
				me.alive = False
				with lock:
					if me in cam.listeners:
						cam.listeners.remove(me)

		def _ws_request(self, me, data):
			try:
				msg = json.loads(data.decode())
				action, props = msg['data']['action'], msg['data'].get('properties') or []
			except (ValueError, KeyError, TypeError):
				return
			if action == 'subscribe':
				with lock:
					new = [p for p in props if p not in me.properties]
					me.properties.update(props)
					values = [(p, cam.read({v: k for k, v in cam.EVENT_NAMES.items()}.get(p, p))) for p in new]
				# like the app: the answer to a subscription carries the current values
				me.send(0x1, json.dumps({'type': 'response', 'id': msg.get('id'),
										 'data': {'action': 'subscribe', 'properties': props, 'success': True,
												  'values': {p: v for p, v in values if v is not None}}}).encode())
			elif action == 'unsubscribe':
				with lock:
					me.properties.difference_update(props)

		# --- the REST API -------------------------------------------------------------

		def do_GET(self):
			if self.path.startswith('/mock/'):
				return self._mock('GET')
			p = self._path()
			if p == '/event/websocket' and 'websocket' in self.headers.get('Upgrade', '').lower():
				if not cam.events or cam.ws_broken:
					return self._send(404, {'error': 'not found'})
				return self._websocket()
			self._arrive('GET', p)
			s = state
			if p == '/event/list' and cam.events:
				return self._send(200, {'events': cam.event_list()})
			with lock:
				value = cam.read(p)
			if value is not None:
				self._send(200, value)
			elif p and p.startswith('/presets/') and p[9:] in s['presets']:
				body = s['presets'][p[9:]]
				self.send_response(200)
				self.send_header('Content-Type', 'application/octet-stream')
				self.send_header('Content-Disposition', 'attachment; filename="{}.bmcpreset"'.format(p[9:]))
				self.send_header('Content-Length', str(len(body)))
				self.send_header('Connection', 'close')
				self.end_headers()
				self.wfile.write(body)
				self.close_connection = True
			else:
				self._send(404, {'error': 'not found'})

		def do_DELETE(self):
			p = self._path()
			self._arrive('DELETE', p)
			with lock:
				if p.startswith('/presets/') and state['presets'].pop(p[9:].replace('.bmcpreset', ''), None) is not None:
					return self._send(204)
			self._send(404, {'error': 'not found'})

		def do_PUT(self):
			self._write('PUT')

		def do_POST(self):
			if self.path.startswith('/mock/'):
				return self._mock('POST')
			self._write('POST')

		def _write(self, method):
			p = self._path()
			body = self._body()
			self._arrive(method, p)
			s = state
			with lock:
				before = cam.snapshot()
				if p.startswith('/presets/') and method == 'PUT':
					name = p[9:].replace('.bmcpreset', '')
					snap = {'appsettings': {'hdmiOut': 0, 'droppedFrames': 'alert'}, 'camerasettings': {'iso': s['iso'], 'wb': s['wb']},
							'secondaryControlsData': {'focusValue': 0.5}}
					s['presets'][name] = json.dumps(snap).encode()
					return self._send(204)
				if p == '/transports/0/record':
					if not cam.deaf:
						cam.set_recording(body.get('recording', True) if method == 'PUT' else True)
				elif p == '/transports/0/stop' and method == 'POST':
					if not cam.deaf:
						cam.set_recording(False)
				elif p == '/video/iso':
					if body['iso'] not in s['supportedISOs']:
						return self._send(400, {'error': 'unsupported ISO'})
					s['iso'] = body['iso']
					s['autoExposure'] = 'Off'   # like the app: a manual ISO or shutter ends auto exposure
				elif p == '/video/shutter':
					if 'shutterSpeed' not in body:
						return self._send(400, {'error': 'this mock only supports shutter speed'})
					s['shutterSpeed'] = body['shutterSpeed']
					s['autoExposure'] = 'Off'
				elif p == '/video/autoExposure':
					if body.get('mode') not in ('Off', 'Continuous', 'OneShot'):
						return self._send(400, {'error': 'bad mode'})
					s['autoExposure'] = body['mode']
				elif p == '/lens/cameras/auto':
					s['autoLens'] = bool(body.get('enabled'))
				elif p == '/video/whiteBalance':
					if not 2500 <= body['whiteBalance'] <= 10000:
						return self._send(400, {'error': 'Invalid white balance temperature'})
					s['wb'] = body['whiteBalance']
				elif p == '/video/whiteBalance/doAuto':
					s['wb'] = 5200
				elif p == '/video/whiteBalanceTint':
					s['tint'] = max(-50, min(50, body['whiteBalanceTint']))
				elif p == '/system/format':
					if s['recording']:
						return self._send(409, {'error': 'cannot change format while recording'})
					s['format'].update(body)
				elif p == '/system/dynamicRange':
					s['dynamicRange'] = body.get('dynamicRange', s['dynamicRange'])
				elif p == '/lens/cameras/active':
					lens = next((l for l in LENSES if l['id'] == body.get('id')), None)
					if lens is None:
						return self._send(400, {'error': 'unknown lens'})
					s['activeLens'] = lens['id']
					s['zoom'] = lens['focalLength']
				elif p == '/lens/iris':
					if not s['irisControllable'] or s['autoExposure'] != 'Off':
						return self._send(403, {'error': 'lens iris is not controllable'})
					if not 1.6 <= body.get('apertureStop', 0) <= 8.0:
						return self._send(400, {'error': 'out of range'})
					s['iris'] = body['apertureStop']
				elif p == '/lens/zoom':
					s['zoom'] = max(13, min(120, body.get('focalLength', s['zoom'])))
				elif p == '/lens/focus':
					s['focus'] = max(0.0, min(1.0, float(body.get('normalised', s['focus']))))
					s['autoFocus'] = False   # like the app: setting focus ends continuous autofocus
				elif p == '/lens/focus/autoFocus':
					s['autoFocus'] = body.get('mode') == 'Continuous' and bool(body.get('enabled', True))
				elif p == '/lens/focus/doAutoFocus':
					pass
				elif p == '/slates/nextClip':
					for k, v in body.items():
						s['slate'].setdefault(k, {}).update(v)
					cam.publish(before)
					return self._send(200, s['slate'])
				elif p.startswith('/monitoring/') and tuple(p.split('/')[2:]) in s['monitor']:
					s['monitor'][tuple(p.split('/')[2:])] = bool(body.get('enabled'))
				elif p == '/livestreams/0/start':
					s['stream'] = 'Streaming'
				elif p == '/livestreams/0/stop':
					s['stream'] = 'Idle'
				else:
					return self._send(404, {'error': 'not found'})
			cam.publish(before)
			self._send(204)

	return Handler


def start(port, name, iris_controllable=True, events=True, verbose=False, host='0.0.0.0'):
	"""Start one mock camera; returns (server, camera)."""
	cam = MockCamera(name, iris_controllable, events)
	server = ThreadingHTTPServer((host, port), make_handler(cam, verbose))
	server.daemon_threads = True
	threading.Thread(target=server.serve_forever, daemon=True).start()
	return server, cam


def main():
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument('--count', type=int, default=1)
	ap.add_argument('--base-port', type=int, default=8081)
	ap.add_argument('--bonjour', action='store_true', help='register each mock with dns-sd (macOS)')
	ap.add_argument('--no-events', action='store_true', help='cameras without the event WebSocket')
	ap.add_argument('-v', '--verbose', action='store_true')
	args = ap.parse_args()

	procs = []
	for i in range(args.count):
		port = args.base_port + i
		name = 'Mock iPhone {}'.format(chr(ord('A') + i))
		# even-numbered mocks have an adjustable iris, odd ones a fixed f/1.78 like most iPhones
		start(port, name, i % 2 == 0, not args.no_events, args.verbose)
		print('{} on http://127.0.0.1:{}'.format(name, port), flush=True)
		if args.bonjour:
			procs.append(subprocess.Popen(['dns-sd', '-R', name, '_http._tcp', 'local', str(port)],
										  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
	try:
		while True:
			time.sleep(1)
	except KeyboardInterrupt:
		pass
	finally:
		for p in procs:
			p.terminate()


if __name__ == '__main__':
	main()
