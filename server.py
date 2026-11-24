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
	ap.add_argument('--log', action='store_true',
					help='write log files: events, and every request sent to the cameras (off by default)')
	ap.add_argument('--log-dir', default=os.path.join(ROOT, 'logs'),
					help='folder for the log files, with --log (default: logs/ next to this script)')
	ap.add_argument('--no-request-log', action='store_true',
					help='with --log: write the events file but not the file listing every request')
	ap.add_argument('--log-keep-days', type=float, default=30,
					help='with --log: delete log files older than this at start, 0 keeps all (default: 30)')
	ap.add_argument('--no-notifications', action='store_true',
					help='no system notification with an alarm (macOS)')
	args = ap.parse_args()

	manager = CameraManager(config_path=args.config, log_dir=args.log_dir if args.log else None,
							log_requests=not args.no_request_log, log_keep_days=args.log_keep_days)
	manager.http_port = args.port
	manager.notify = not args.no_notifications

	class Handler(BaseHTTPRequestHandler):
		def log_message(self, fmt, *a):
			pass

		def _handle(self, method):
			n = int(self.headers.get('Content-Length') or 0)
			body = self.rfile.read(n) if n else b''
			status, ctype, data, extra = manager.handle_http(method, self.path, body)
			self.send_response(status)
			self.send_header('Content-Type', ctype)
			for k, v in extra.items():
				self.send_header(k, v)
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
	if manager.files and manager.files.path:
		print('Log: {}'.format(manager.files.path), flush=True)
		manager._log('started on {}'.format(url))
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
