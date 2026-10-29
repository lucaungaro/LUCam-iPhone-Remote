#!/usr/bin/env python3
"""LUCam iPhone Remote: control iPhones running Blackmagic Camera from a web page.

    python3 server.py [--port 9980] [--open]

Then open http://localhost:9980, or http://<this machine's IP>:9980 from any
device on the network.
"""

import argparse
import os
import signal
import socketserver
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'python'))

from lucam import CameraManager  # noqa: E402


class Server(ThreadingHTTPServer):
	daemon_threads = True

	def server_bind(self):
		# HTTPServer.server_bind does a reverse DNS lookup of this machine
		# (socket.getfqdn), which can hang for a long time on some networks.
		socketserver.TCPServer.server_bind(self)
		self.server_name, self.server_port = 'localhost', self.server_address[1]


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument('--port', type=int, default=9980)
	ap.add_argument('--config', default=os.path.join(ROOT, 'cameras.json'),
					help='camera list file (default: cameras.json next to this script)')
	ap.add_argument('--open', action='store_true', help='open the control panel in the browser')
	args = ap.parse_args()

	manager = CameraManager(config_path=args.config)

	class Handler(BaseHTTPRequestHandler):
		def log_message(self, fmt, *a):
			pass

		def _handle(self, method):
			n = int(self.headers.get('Content-Length') or 0)
			body = self.rfile.read(n) if n else b''
			status, ctype, data = manager.handle_http(method, self.path, body)
			self.send_response(status)
			self.send_header('Content-Type', ctype)
			self.send_header('Content-Length', str(len(data)))
			self.send_header('Cache-Control', 'no-store')
			self.end_headers()
			self.wfile.write(data)

		def do_GET(self):
			self._handle('GET')

		def do_POST(self):
			self._handle('POST')

	def on_term(*_):
		raise KeyboardInterrupt
	signal.signal(signal.SIGTERM, on_term)  # stop MediaMTX too when killed

	server = Server(('0.0.0.0', args.port), Handler)
	url = 'http://localhost:{}/'.format(args.port)
	print('LUCam iPhone Remote on {}  (Ctrl-C to quit)'.format(url), flush=True)
	if args.open:
		threading.Timer(0.5, webbrowser.open, args=(url,)).start()
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		manager.shutdown()


if __name__ == '__main__':
	main()
