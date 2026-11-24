"""The camera's event channel: a WebSocket that pushes every change of state.

    wss://<phone>:4444/control/api/v1/event/websocket

Why it matters. Blackmagic Camera for iOS 3.5 slows down, over hours, when its
REST API is used while no client is connected to this WebSocket: each request is
then followed by a pass over the app's whole state, its answers take longer and
longer, and recordings end up dropping frames. Only relaunching the app clears
it. With a client connected from the start, none of this was seen. So the
controller opens this channel before it sends a camera anything, keeps it open,
and takes the camera's state from it instead of polling.

Protocol (from the API reference the phone serves, notification.yaml 1.2.0):

    -> {"type": "request", "id": 1, "data": {"action": "subscribe", "properties": ["/video/iso", ...]}}
    <- {"type": "response", "id": 1, "data": {"action": "subscribe", "properties": [...], "values": {...}, "success": true}}
    <- {"type": "event", "data": {"action": "propertyValueChanged", "property": "/video/iso", "value": {"iso": 400}}}

An event's value is the object a GET of the same path returns. Only the
standard library is used.
"""

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time

from . import api

WS_PATH = api.API_ROOT + '/event/websocket'
_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

# What the controller shows or acts on. The channel subscribes to these the
# moment it opens; the worker then adds everything else the camera lists under
# /event/list, which is the subscription the long tests ran with.
PROPERTIES = (
	'/transports/0/record', '/transports/0/timecode',
	'/video/iso', '/video/supportedISOs', '/video/shutter', '/video/supportedShutters',
	'/video/whiteBalance', '/video/whiteBalance/description',
	'/video/whiteBalanceTint', '/video/whiteBalanceTint/description',
	'/system/product', '/system/format', '/system/supportedFormats',
	'/system/dynamicRange', '/system/supportedDynamicRanges',
	'/lens/iris', '/lens/iris/description', '/lens/focus', '/lens/focus/description',
	'/lens/zoom', '/lens/zoom/description', '/lens/cameras', '/lens/cameras/active',
	'/slates/nextClip', '/livestreams/0', '/livestreams/0/activePlatform',
	'/monitoring/LCD/displayLUT', '/monitoring/HDMI/displayLUT',
	'/monitoring/LCD/cleanFeed', '/monitoring/HDMI/cleanFeed',
)


class Refused(Exception):
	"""An HTTP server answered but would not open the event channel."""


def _recv_exact(sock, n):
	buf = b''
	while len(buf) < n:
		chunk = sock.recv(n - len(buf))
		if not chunk:
			raise OSError('connection closed')
		buf += chunk
	return buf


def connect(scheme, host, port, timeout=3.0):
	"""Open the WebSocket; returns the connected socket. Raises OSError when
	nothing answers, Refused when a server answers without upgrading."""
	raw = socket.create_connection((host, port), timeout=timeout)
	try:
		raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		sock = api._UNVERIFIED_SSL.wrap_socket(raw, server_hostname=host) if scheme == 'https' else raw
	except Exception:
		raw.close()
		raise
	try:
		key = base64.b64encode(os.urandom(16)).decode()
		h = '[{}]'.format(host) if ':' in host else host
		sock.sendall(('GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
					  'Sec-WebSocket-Key: {}\r\nSec-WebSocket-Version: 13\r\n\r\n').format(WS_PATH, h, port, key).encode())
		head = b''
		while b'\r\n\r\n' not in head:
			chunk = sock.recv(4096)
			if not chunk:
				raise OSError('closed during the handshake')
			head += chunk
			if len(head) > 65536:
				raise Refused('oversized answer to the handshake')
		lines = head.split(b'\r\n\r\n', 1)[0].decode('latin-1').split('\r\n')
		status = lines[0].split(' ')
		if len(status) < 2 or status[1] != '101':
			raise Refused(lines[0][:80])
		headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(':') for l in lines[1:])}
		want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
		if headers.get('sec-websocket-accept', want) != want:
			raise Refused('bad Sec-WebSocket-Accept')
		rest = head.split(b'\r\n\r\n', 1)[1]
		return sock, rest
	except Exception:
		try:
			sock.close()
		except OSError:
			pass
		raise


def _frame(opcode, payload=b''):
	"""One masked client frame."""
	mask = os.urandom(4)
	n = len(payload)
	head = bytearray([0x80 | opcode])
	if n < 126:
		head.append(0x80 | n)
	elif n < 65536:
		head.append(0x80 | 126)
		head += struct.pack('>H', n)
	else:
		head.append(0x80 | 127)
		head += struct.pack('>Q', n)
	return bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class EventChannel(threading.Thread):
	"""Keeps one WebSocket to a camera open, for as long as the worker lives.

	candidates: addresses to try in turn until one opens (a bare IP typed by
	the user becomes https://ip:4444, http://ip, https://ip).

	Callbacks, all called on this thread:
	  on_open(address)        the channel is open and subscribed
	  on_close(reason)        it was open and is not any more
	  on_refused(address)     a server answered there without an event channel
	  on_event(property, value)

	Reconnects by itself: at once after a drop, then every RETRY seconds (RETRY_REFUSED
	on a server that answers without opening the channel).
	"""

	RETRY = 1.0            # between attempts while the camera does not answer
	RETRY_REFUSED = 5.0    # between attempts on a server that does not open the event channel

	def __init__(self, candidates, on_event, on_open, on_close, on_refused, on_unreachable=None, name='lucam-events'):
		super().__init__(name=name, daemon=True)
		self.candidates = list(candidates)
		self.on_event, self.on_open, self.on_close, self.on_refused = on_event, on_open, on_close, on_refused
		self.on_unreachable = on_unreachable   # nothing answered at any candidate (called at every attempt)
		self._halt = threading.Event()
		self._wake = threading.Event()
		self._send_lock = threading.Lock()
		self._sock = None
		self._next_id = 1
		self.connected = False
		self.address = None      # the candidate that opened
		self.last_rx = 0.0       # monotonic time of the last frame received (any kind)
		self.pongs = 0           # pongs received on this connection: tells whether the camera answers pings
		self.messages = 0        # events received since the worker started
		self.extra = []          # properties to subscribe to on top of PROPERTIES (kept across reconnects)

	# --- called from other threads ------------------------------------------

	def stop(self):
		self._halt.set()
		self._wake.set()
		self._drop()

	def reset(self):
		"""Drop the connection (it will be reopened at once)."""
		self._drop()

	def retry_now(self):
		self._wake.set()

	def ping(self):
		return self._send(_frame(0x9, b'lucam'))

	def subscribe(self, properties):
		"""Add properties to the subscription, now and after every reconnect."""
		new = [p for p in properties if p not in PROPERTIES and p not in self.extra]
		self.extra.extend(new)
		if new and self.connected:
			self._request('subscribe', new)

	# --- internals -----------------------------------------------------------

	def _drop(self):
		sock = self._sock
		if sock is not None:
			try:
				sock.shutdown(socket.SHUT_RDWR)
			except OSError:
				pass
			try:
				sock.close()
			except OSError:
				pass

	def _send(self, data):
		sock = self._sock
		if sock is None:
			return False
		try:
			with self._send_lock:
				sock.sendall(data)
			return True
		except OSError:
			return False

	def _request(self, action, properties):
		msg = {'type': 'request', 'id': self._next_id, 'data': {'action': action, 'properties': list(properties)}}
		self._next_id += 1
		return self._send(_frame(0x1, json.dumps(msg).encode()))

	def run(self):
		short = 0   # connections in a row that closed within 2 s of opening
		while not self._halt.is_set():
			refused = None
			opened = False
			for address in ([self.address] if self.address else self.candidates):
				if self._halt.is_set():
					return
				scheme, host, port = api.parse_address(address)
				try:
					sock, rest = connect(scheme, host, port)
				except Refused:
					refused = refused or address
					continue
				except (OSError, ValueError):
					continue
				opened = True
				since = time.monotonic()
				self._serve(sock, rest, address)
				break
			if self._halt.is_set():
				return
			if opened:
				# It was open and dropped: try again at once. Unless it keeps closing as
				# soon as it opens, which must not turn into a storm of reconnections.
				short = short + 1 if time.monotonic() - since < 2.0 else 0
				if short >= 2:
					self._wake.wait(self.RETRY)
					self._wake.clear()
				continue
			try:
				if refused:
					self.on_refused(refused)
				elif self.on_unreachable:
					self.on_unreachable()
			except Exception:
				pass
			self._wake.wait(self.RETRY_REFUSED if refused else self.RETRY)
			self._wake.clear()

	def _serve(self, sock, rest, address):
		self._sock = sock
		self.address = address
		self.pongs = 0
		self.last_rx = time.monotonic()
		reason = 'closed'
		try:
			sock.settimeout(None)
			if not self._request('subscribe', list(PROPERTIES) + self.extra):
				raise OSError('could not subscribe')
			self.connected = True
			self.on_open(address)
			buf = bytearray(rest)
			message = bytearray()   # a fragmented text message being assembled
			while not self._halt.is_set():
				frame = self._read_frame(sock, buf)
				self.last_rx = time.monotonic()
				fin, opcode, payload = frame
				if opcode == 0x9:
					self._send(_frame(0xA, payload))
				elif opcode == 0xA:
					self.pongs += 1
				elif opcode == 0x8:
					self._send(_frame(0x8, payload[:2]))
					reason = 'closed by the camera'
					break
				elif opcode in (0x0, 0x1, 0x2):
					message += payload
					if fin:
						self._dispatch(bytes(message))
						message = bytearray()
		except OSError as e:
			reason = str(e) or 'connection lost'
		finally:
			was = self.connected
			self.connected = False
			self._sock = None
			try:
				sock.close()
			except OSError:
				pass
			if was and not self._halt.is_set():
				try:
					self.on_close(reason)
				except Exception:
					pass

	@staticmethod
	def _read_frame(sock, buf):
		def need(n):
			while len(buf) < n:
				chunk = sock.recv(65536)
				if not chunk:
					raise OSError('connection closed')
				buf.extend(chunk)
		need(2)
		fin, opcode, masked, n = bool(buf[0] & 0x80), buf[0] & 0x0F, bool(buf[1] & 0x80), buf[1] & 0x7F
		off = 2
		if n == 126:
			need(4)
			n = struct.unpack('>H', buf[2:4])[0]
			off = 4
		elif n == 127:
			need(10)
			n = struct.unpack('>Q', buf[2:10])[0]
			off = 10
		if n > 16 * 1024 * 1024:
			raise OSError('oversized frame')
		mask = None
		if masked:
			need(off + 4)
			mask = bytes(buf[off:off + 4])
			off += 4
		need(off + n)
		payload = bytes(buf[off:off + n])
		del buf[:off + n]
		if mask:
			payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
		return fin, opcode, payload

	# Not named _handle: threading.Thread has an attribute of that name from Python 3.13 on,
	# which would replace the method and make every message fail.
	def _dispatch(self, raw):
		try:
			msg = json.loads(raw.decode('utf-8'))
		except (ValueError, UnicodeDecodeError):
			return
		if not isinstance(msg, dict):
			return
		data = msg.get('data')
		if not isinstance(data, dict):
			return
		if msg.get('type') == 'event' and data.get('action') == 'propertyValueChanged':
			self._deliver(data.get('property'), data.get('value'))
		elif msg.get('type') == 'response' and isinstance(data.get('values'), dict):
			# the answer to a subscription carries the current values
			for prop, value in data['values'].items():
				self._deliver(prop, value)

	def _deliver(self, prop, value):
		if not isinstance(prop, str):
			return
		self.messages += 1
		try:
			self.on_event(prop, value)
		except Exception:
			pass   # a bad event must not close the channel


class listening:
	"""Hold a camera's event channel open while a script sends it requests:

	    with events.listening('192.168.1.20') as is_open:
	        ...requests...

	`is_open` is False when the channel could not be opened within `wait` seconds
	(the camera has none, or does not answer). With `exact`, only the address as
	given is tried, not its https/http variants."""

	def __init__(self, address, wait=3.0, exact=False):
		self.wait = wait
		self._opened = threading.Event()
		self._settled = threading.Event()   # open, refused or unreachable: no need to wait longer
		self._channel = EventChannel([address] if exact else api.address_candidates(address),
									 on_event=lambda prop, value: None, on_open=lambda a: self._settle(True),
									 on_close=lambda reason: None, on_refused=lambda a: self._settle(False),
									 on_unreachable=lambda: self._settle(False), name='lucam-events-tool')

	def _settle(self, opened):
		if opened:
			self._opened.set()
		self._settled.set()

	def __enter__(self):
		self._channel.start()
		self._settled.wait(self.wait)
		return self._opened.is_set()

	def __exit__(self, *exc):
		self._channel.stop()
		return False


def probe(address, timeout=1.0):
	"""api.probe with the camera's event channel held open around it, so that even
	the request that finds a camera reaches an app that is being listened to."""
	with listening(address, wait=timeout, exact=True):
		return api.probe(address, timeout=timeout)
