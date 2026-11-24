"""Writes what the app does to files, so a shooting day can be reconstructed afterwards.
Off unless the controller is started with --log.

Two files per run, in the log folder:

  lucam-YYYYMMDD-HHMMSS.log           events: the same lines as the panel's log (cameras found,
                                      values changed, values restored, errors), plus every write
                                      sent to a camera with the value it carried, and requests
                                      that failed (a camera that stops answering gets one line
                                      when it stops and one when it answers again)
  lucam-YYYYMMDD-HHMMSS.requests.csv  every request sent to every camera, with the time it was
                                      sent, what a write carried, how long the camera took to
                                      answer, and the status

The requests file is for forensics: laid over a clip's timeline it shows what the app asked a
phone at any moment, and how each phone's answer time moved through the day. Cameras are
listened to, not polled, so the file stays small: a line per command, plus one timing read per
armed camera every 30 s. Files older than keep_days are deleted when a run starts.

Times are this Mac's clock, not the phones' timecode: see the README before lining them up with
clips. Writing is thread-safe; nothing here can raise into the caller (a full disk only stops
the log).
"""

import csv
import glob
import os
import threading
import time


def _stamp(t):
	return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t)) + '.{:03d}'.format(int(t * 1000) % 1000)


def delete_old(folder, keep_days):
	"""Delete this app's log files older than keep_days; returns how many went."""
	if not keep_days or keep_days <= 0:
		return 0
	limit = time.time() - keep_days * 86400
	gone = 0
	for path in glob.glob(os.path.join(folder, 'lucam-*.log')) + glob.glob(os.path.join(folder, 'lucam-*.requests.csv')):
		try:
			if os.path.getmtime(path) < limit:
				os.remove(path)
				gone += 1
		except OSError:
			pass
	return gone


class FileLog:
	def __init__(self, folder, requests=True, keep_days=30):
		self.folder = folder
		self.lock = threading.Lock()
		self.events = None
		self.requests = None
		self._req_writer = None
		self._silent = {}   # camera -> [first failure time, failed requests since] while it doesn't answer
		self.path = self.requests_path = None
		try:
			os.makedirs(folder, exist_ok=True)
			deleted = delete_old(folder, keep_days)
			name = time.strftime('lucam-%Y%m%d-%H%M%S')
			self.path = os.path.join(folder, name + '.log')
			self.events = open(self.path, 'a', encoding='utf-8')
			if requests:
				self.requests_path = os.path.join(folder, name + '.requests.csv')
				self.requests = open(self.requests_path, 'a', newline='', encoding='utf-8')
				self._req_writer = csv.writer(self.requests)
				self._req_writer.writerow(['sent_at', 'camera', 'method', 'path', 'sent', 'status', 'took_ms', 'error'])
			if deleted:
				self.event('deleted {} log file(s) older than {:g} days'.format(deleted, keep_days))
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

	def request(self, t, camera, method, path, status, took, error='', sent=''):
		"""One request to a camera: in the requests file, and in the events file if it was a
		write or it failed. `status` is the HTTP status, 0 when the camera did not answer;
		`sent` is what a write carried."""
		if self.requests:
			with self.lock:
				try:
					self._req_writer.writerow([_stamp(t), camera, method, path, sent, status,
											   '{:.0f}'.format(took * 1000), error])
					self.requests.flush()
				except (OSError, ValueError):
					pass
		if status == 0:
			# No answer: one line when the camera goes silent, then nothing until it answers
			# again (an offline camera is retried every 2 s, on several addresses).
			with self.lock:
				first = camera not in self._silent
				if first:
					self._silent[camera] = [t, 0]
				self._silent[camera][1] += 1
			if first:
				self.event('{}: not answering ({} {}: {})'.format(camera, method, path, error or 'no answer'), t)
			if method == 'GET':
				return
		else:
			with self.lock:
				silent = self._silent.pop(camera, None)
			if silent:
				self.event('{}: answering again after {:.0f} s ({} requests without an answer)'.format(
					camera, t - silent[0], silent[1]), t)
		# A GET answered 4xx is the controller finding a feature this phone lacks, not a failure.
		if method != 'GET' or status >= 500:
			self.event('{}: {} {}{} -> {}{}'.format(camera, method, path, ' ' + sent if sent else '',
												  status or 'no answer', ' ({})'.format(error) if error else ''), t)

	def close(self):
		with self.lock:
			for f in (self.events, self.requests):
				if f:
					try:
						f.close()
					except OSError:
						pass
			self.events = self.requests = None
