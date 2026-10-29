"""Live view: the camera pushes its live stream (RTMP) to a MediaMTX server
on this machine, which re-serves it to browsers over WebRTC (no re-encoding,
~0.2-0.5 s latency).

MediaMTX (https://mediamtx.org, `brew install mediamtx`) is started on first
use and stopped with the controller. An instance already running with its API
on 127.0.0.1:9997 is reused instead.
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import urllib.request

RTMP_PORT = 1935
WEBRTC_PORT = 8889
API = 'http://127.0.0.1:9997'

PLATFORM_NAME = 'LUCam iPhone Remote'          # the phone lists it as "LUCam iPhone Remote RTMP"
PLATFORM_ID = PLATFORM_NAME + ' RTMP'
PLATFORM_FILE = 'LUCamiPhoneRemote.xml'
LEGACY_PLATFORMS = ('BM Cam Remote RTMP',)  # from before the rename; removed from phones by name
PROFILE = 'Preview'

# The app only streams when a profile config matches the camera's current
# resolution and frame rate. It rejects fractional rates (23.98, 29.97...).
STREAM_RATES = ('24', '25', '30', '48', '50', '60')
STREAM_BITRATES = {'2160p': 12000000, '1080p': 6000000, '720p': 3000000}

MEDIAMTX_CONFIG = """\
logLevel: warn
readTimeout: 30s
api: yes
apiAddress: 127.0.0.1:9997
rtsp: no
hls: no
srt: no
moq: no
rtmp: yes
rtmpAddress: :{rtmp}
webrtc: yes
webrtcAddress: :{webrtc}
webrtcLocalUDPAddress: :8189
paths:
  all_others:
"""


def platform_xml(rtmp_url):
	configs = ''.join(
		'<config resolution="{}" fps="{}"><bitrate>{}</bitrate><audio-bitrate>128000</audio-bitrate>'
		'<keyframe-interval>1</keyframe-interval></config>'.format(res, fps, bitrate)
		for res, bitrate in STREAM_BITRATES.items() for fps in STREAM_RATES)
	return (
		'<?xml version="1.0" encoding="UTF-8"?><streaming><service>'
		'<name>{}</name><servers><server><name>Mac</name><url>{}</url></server></servers>'
		'<profiles><profile><name>{}</name><low-latency/>{}</profile></profiles>'
		'</service></streaming>').format(PLATFORM_NAME, rtmp_url, PROFILE, configs)


def local_ip_toward(host):
	"""IP of this machine on the interface that reaches `host`."""
	s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	try:
		s.connect((socket.gethostbyname(host), 9))
		return s.getsockname()[0]
	finally:
		s.close()


def find_mediamtx():
	return shutil.which('mediamtx') or next(
		(p for p in ('/opt/homebrew/bin/mediamtx', '/usr/local/bin/mediamtx') if os.path.exists(p)), None)


class LiveServer:
	def __init__(self):
		self._proc = None
		self._lock = threading.Lock()
		self._config_path = None

	def _api(self, path, timeout=1.0):
		with urllib.request.urlopen(API + path, timeout=timeout) as resp:
			return json.loads(resp.read().decode('utf-8'))

	def running(self):
		try:
			self._api('/v3/paths/list')
			return True
		except (OSError, ValueError):
			return False

	def ensure_running(self):
		"""Start MediaMTX unless one is already answering. Raises RuntimeError."""
		with self._lock:
			if self.running():
				return
			binary = find_mediamtx()
			if not binary:
				raise RuntimeError('MediaMTX is not installed (brew install mediamtx)')
			if not self._config_path:
				fd, self._config_path = tempfile.mkstemp(prefix='lucam-mediamtx-', suffix='.yml')
				with os.fdopen(fd, 'w') as f:
					f.write(MEDIAMTX_CONFIG.format(rtmp=RTMP_PORT, webrtc=WEBRTC_PORT))
			self._proc = subprocess.Popen([binary, self._config_path],
										  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
			for _ in range(30):
				if self._proc.poll() is not None:
					err = self._proc.stderr.read().decode('utf-8', 'replace').strip().splitlines()
					raise RuntimeError('MediaMTX exited: ' + (err[-1] if err else 'unknown error'))
				if self.running():
					return
				threading.Event().wait(0.1)
			raise RuntimeError('MediaMTX did not start')

	def paths(self):
		"""{path_name: {'ready': bool, 'tracks': [...], 'bytes': int}}"""
		try:
			items = self._api('/v3/paths/list').get('items', [])
		except (OSError, ValueError):
			return {}
		return {p['name']: {'ready': p.get('ready', False), 'tracks': p.get('tracks', []),
							'bytes': p.get('bytesReceived', 0)} for p in items}

	def stop(self):
		with self._lock:
			if self._proc and self._proc.poll() is None:
				self._proc.terminate()
				try:
					self._proc.wait(3)
				except subprocess.TimeoutExpired:
					self._proc.kill()
			self._proc = None
			if self._config_path and os.path.exists(self._config_path):
				os.remove(self._config_path)
				self._config_path = None
