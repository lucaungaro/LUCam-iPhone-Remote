#!/usr/bin/env python3
"""Simulated Blackmagic camera(s) speaking the REST API, for testing without
iPhones. Each instance listens on its own port and can register itself with
Bonjour (macOS `dns-sd`) so discovery can be tested too.

    python3 tools/mock_camera.py --count 3 --base-port 8081 --bonjour
"""

import argparse
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = '/control/api/v1'
CODECS = ['Apple ProRes 422 HQ:Default', 'Apple ProRes 422:Default', 'HEVC (H.265):High', 'H.264:High', 'H.264:Medium']


def make_state(name):
	return {
		'product': {'deviceName': name, 'productName': 'Blackmagic Camera (iPhone, simulated)', 'softwareVersion': '3.5.3'},
		'recording': False,
		'recStart': 0,
		'iso': 400,
		'supportedISOs': [50, 100, 200, 400, 800, 1250, 1600, 2000, 3200],
		'shutterSpeed': 50,
		'wb': 5600, 'tint': 0,
		'format': {'codec': 'Apple ProRes 422 HQ:Default', 'frameRate': '25',
				   'recordResolution': {'width': 3840, 'height': 2160}, 'sensorResolution': {'width': 3840, 'height': 2160},
				   'resolutionDescriptor': {'group': '4K', 'aspectRatio': '16:9', 'description': '4K'},
				   'offSpeedEnabled': False},
		'zoom': 24,
		'slate': {'clip': {'clipName': 'A001_C001', 'reel': 1, 'scene': '1', 'take': 1},
				  'project': {'projectName': 'Mock', 'camera': 'A'}},
		'stream': 'Idle',
	}


def make_handler(state, lock, verbose):
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
			self.end_headers()
			self.wfile.write(body)

		def _body(self):
			n = int(self.headers.get('Content-Length') or 0)
			return json.loads(self.rfile.read(n)) if n else {}

		def _path(self):
			if not self.path.startswith(ROOT):
				return None
			return self.path[len(ROOT):].split('?')[0]

		def do_GET(self):
			p = self._path()
			s = state
			with lock:
				tc_secs = int(time.time() - s['recStart']) if s['recording'] else 0
				routes = {
					'/system/product': s['product'],
					'/system/format': s['format'],
					'/system/supportedFormats': {'supportedFormats': [{
						'codecs': CODECS,
						'frameRates': ['23.98', '24', '25', '29.97', '30', '48', '50', '59.94', '60'],
						'recordResolution': {'width': w, 'height': h}, 'sensorResolution': {'width': w, 'height': h},
						'resolutionDescriptor': {'group': d, 'aspectRatio': '16:9', 'description': d}}
						for w, h, d in ((3840, 2160, '4K'), (1920, 1080, 'HD'))]},
					'/transports/0': {'mode': 'InputRecord' if s['recording'] else 'InputPreview'},
					'/transports/0/record': {'recording': s['recording']},
					'/transports/0/timecode': {'display': '00:00:{:02d}:{:02d}'.format(tc_secs // 60 % 60, tc_secs % 60)},
					'/video/iso': {'iso': s['iso']},
					'/video/supportedISOs': {'supportedISOs': s['supportedISOs']},
					'/video/shutter': {'continuousShutterAutoExposure': False, 'shutterSpeed': s['shutterSpeed']},
					'/video/supportedShutters': {'shutterSpeeds': [24, 25, 30, 48, 50, 60, 100, 120, 200, 250, 500, 1000]},
					'/video/whiteBalance': {'whiteBalance': s['wb']},
					'/video/whiteBalance/description': {'whiteBalance': {'min': 2500, 'max': 10000}},
					'/video/whiteBalanceTint': {'whiteBalanceTint': s['tint']},
					'/video/whiteBalanceTint/description': {'whiteBalanceTint': {'min': -50, 'max': 50}},
					'/lens/zoom': {'focalLength': s['zoom'], 'normalised': 0.1},
					'/lens/zoom/description': {'controllable': True, 'focalLength': {'adjustable': True, 'min': 13, 'max': 120}},
					'/slates/nextClip': s['slate'],
					'/livestreams/0': {'status': s['stream'], 'bitrate': 0, 'effectiveVideoFormat': '1080p25'},
				}
			if p in routes:
				self._send(200, routes[p])
			else:
				self._send(404, {'error': 'not found'})

		def do_PUT(self):
			self._write('PUT')

		def do_POST(self):
			self._write('POST')

		def _write(self, method):
			p = self._path()
			body = self._body()
			s = state
			with lock:
				if p == '/transports/0/record':
					rec = body.get('recording', True) if method == 'PUT' else True
					if rec and not s['recording']:
						s['recStart'] = time.time()
						c = s['slate']['clip']
						c['clipName'] = '{}{:03d}_C{:03d}'.format(s['slate']['project']['camera'], c['reel'], c['take'])
					if not rec and s['recording']:
						s['slate']['clip']['take'] += 1
					s['recording'] = rec
				elif p == '/transports/0/stop' and method == 'POST':
					if s['recording']:
						s['slate']['clip']['take'] += 1
					s['recording'] = False
				elif p == '/video/iso':
					if body['iso'] not in s['supportedISOs']:
						return self._send(400, {'error': 'unsupported ISO'})
					s['iso'] = body['iso']
				elif p == '/video/shutter':
					if 'shutterSpeed' not in body:
						return self._send(400, {'error': 'this mock only supports shutter speed'})
					s['shutterSpeed'] = body['shutterSpeed']
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
				elif p == '/lens/zoom':
					s['zoom'] = max(13, min(120, body.get('focalLength', s['zoom'])))
				elif p == '/slates/nextClip':
					for k, v in body.items():
						s['slate'].setdefault(k, {}).update(v)
					return self._send(200, s['slate'])
				elif p == '/livestreams/0/start':
					s['stream'] = 'Streaming'
				elif p == '/livestreams/0/stop':
					s['stream'] = 'Idle'
				else:
					return self._send(404, {'error': 'not found'})
			self._send(204)

	return Handler


def main():
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument('--count', type=int, default=1)
	ap.add_argument('--base-port', type=int, default=8081)
	ap.add_argument('--bonjour', action='store_true', help='register each mock with dns-sd (macOS)')
	ap.add_argument('-v', '--verbose', action='store_true')
	args = ap.parse_args()

	procs = []
	for i in range(args.count):
		port = args.base_port + i
		name = 'Mock iPhone {}'.format(chr(ord('A') + i))
		server = ThreadingHTTPServer(('0.0.0.0', port), make_handler(make_state(name), threading.Lock(), args.verbose))
		threading.Thread(target=server.serve_forever, daemon=True).start()
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
