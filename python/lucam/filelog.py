"""Writes what the app does to files, so a shooting day can be reconstructed afterwards.

Two files per run, in the log folder:

  lucam-YYYYMMDD-HHMMSS.log           events: the same lines as the panel's log (cameras found,
                                      values changed, values restored, errors), plus every write
                                      sent to a camera and every request that failed
  lucam-YYYYMMDD-HHMMSS.requests.csv  every request sent to every camera, with the time it was
                                      sent, how long the camera took to answer, and the status

The requests file is for forensics: laid over a clip's timeline it shows what the app asked a
phone at any moment. At 16 cameras polled twice a second it grows by about 25 MB an hour.
Writing is thread-safe; nothing here can raise into the caller (a full disk only stops the log).
"""

import csv
import os
import threading
import time


def _stamp(t):
	return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t)) + '.{:03d}'.format(int(t * 1000) % 1000)


class FileLog:
	def __init__(self, folder, requests=True):
		self.folder = folder
		self.lock = threading.Lock()
		self.events = None
		self.requests = None
		self._req_writer = None
		self.path = self.requests_path = None
		try:
			os.makedirs(folder, exist_ok=True)
			name = time.strftime('lucam-%Y%m%d-%H%M%S')
			self.path = os.path.join(folder, name + '.log')
			self.events = open(self.path, 'a', encoding='utf-8')
			if requests:
				self.requests_path = os.path.join(folder, name + '.requests.csv')
				self.requests = open(self.requests_path, 'a', newline='', encoding='utf-8')
				self._req_writer = csv.writer(self.requests)
				self._req_writer.writerow(['sent_at', 'camera', 'method', 'path', 'status', 'took_ms', 'error'])
		except OSError:
			self.events = self.requests = None

	def event(self, msg, t=None):
		"""One line in the events file."""
		if not self.events:
			return
		line = '{} {}\n'.format(_stamp(t or time.time()), msg)
		with self.lock:
			try:
				self.events.write(line)
				self.events.flush()
			except (OSError, ValueError):
				pass

	def request(self, t, camera, method, path, status, took, error=''):
		"""One request to a camera: in the requests file, and in the events file if it was a
		write or it failed. `status` is the HTTP status, 0 when the camera did not answer."""
		if self.requests:
			with self.lock:
				try:
					self._req_writer.writerow([_stamp(t), camera, method, path, status, '{:.0f}'.format(took * 1000), error])
					self.requests.flush()
				except (OSError, ValueError):
					pass
		if method != 'GET' or error:
			self.event('{}: {} {} -> {}{}'.format(camera, method, path, status or 'no answer',
												 ' ({})'.format(error) if error else ''), t)

	def close(self):
		with self.lock:
			for f in (self.events, self.requests):
				if f:
					try:
						f.close()
					except OSError:
						pass
			self.events = self.requests = None
