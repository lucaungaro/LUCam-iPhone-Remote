#!/usr/bin/env python3
"""Checks the controller against simulated cameras that count what they are sent.

    python3 tests/test_engine.py

The rules under test: the event channel is open before anything is sent; nothing
is read on a timer while it is open; a camera without the channel is polled and says
so; the alarms fire. Standard library only.
"""

import json
import os
import sys
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'python'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import mock_camera  # noqa: E402
from lucam import CameraManager, events  # noqa: E402
from lucam.manager import CameraWorker  # noqa: E402

# Shorter than on set, so the suite runs in under a minute.
FAST = {'HEALTH_INTERVAL': 3600.0, 'REC_GRACE': 0.3, 'REC_CONFIRM': 1.0, 'LOST_WHILE_RECORDING': 1.0,
		'TICK': 0.1, 'WB_RESTORE_EVERY': 0.3, 'BACK_OFF': 0.5,
		'POLL_AFTER_DROP': 0.5, 'POLLED_ALARM_AFTER': 1.0, 'RETRY_OFFLINE': 0.5}
events.EventChannel.RETRY_REFUSED = 0.5


def wait(cond, timeout=6.0):
	end = time.time() + timeout
	while time.time() < end:
		if cond():
			return True
		time.sleep(0.03)
	return bool(cond())


class Rig:
	"""One simulated camera and a controller connected to it."""

	def __init__(self, events=True, ws_broken=False, **settings):
		self.saved = {k: getattr(CameraWorker, k) for k in dict(FAST, **settings)}
		for k, v in dict(FAST, **settings).items():
			setattr(CameraWorker, k, v)
		self.server, self.cam = mock_camera.start(0, 'Mock', True, events, host='127.0.0.1')
		self.cam.ws_broken = ws_broken
		self.port = self.server.server_address[1]
		self.manager = CameraManager(config_path=None, autosave=False)
		self.manager.notify = False
		self.id = self.manager.add_camera('http://127.0.0.1:{}'.format(self.port))

	def close(self):
		self.manager.shutdown()
		self.server.shutdown()
		self.server.server_close()
		for k, v in self.saved.items():
			setattr(CameraWorker, k, v)

	def state(self):
		return self.manager._worker(self.id).snapshot()

	def stats(self):
		return self.mock('GET', 'stats')

	def mock(self, method, what, body=None):
		req = urllib.request.Request('http://127.0.0.1:{}/mock/{}'.format(self.port, what), method=method,
									 data=json.dumps(body or {}).encode() if method == 'POST' else None)
		with urllib.request.urlopen(req, timeout=3) as r:
			raw = r.read()
		return json.loads(raw) if raw else None

	def log(self):
		return ' | '.join(e['msg'] for e in self.manager.log)

	def ready(self):
		ok = wait(lambda: self.state()['online'] and self.state()['iso'] is not None and self.state()['letter'])
		time.sleep(0.3)   # let the subscription to the full event list go out
		return ok


class EngineTest(unittest.TestCase):
	def rig(self, **kw):
		r = Rig(**kw)
		self.addCleanup(r.close)
		self.assertTrue(r.ready(), 'camera did not come online: ' + r.log())
		return r

	def test_listens_first_then_reads_once(self):
		r = self.rig()
		s, st = r.state(), r.stats()
		self.assertEqual(s['events'], 'listening')
		self.assertEqual(st['listeners'], 1)
		self.assertEqual(st['withoutListener'], 0, 'a request was sent before the event channel was open')
		self.assertEqual((s['iso'], s['wb'], s['letter'], s['fps']), (400, 5600, 'A', '25'))
		self.assertTrue(s['lensOptions'] and s['isoOptions'] and s['resolutionOptions'])
		self.assertEqual(s['displayLuts'], {'Device': False, 'HDMI': False})
		self.assertIsNotNone(s['answerMs'])
		self.assertEqual(s['health'], 'ok')

	def test_nothing_is_read_on_a_timer(self):
		r = self.rig()
		before = r.stats()['requests']
		tc = r.state()['timecode']
		time.sleep(3.0)
		self.assertEqual(r.stats()['requests'], before, 'requests were sent to an idle camera')
		self.assertNotEqual(r.state()['timecode'], tc, 'the timecode did not follow the events')

	def test_a_change_on_the_phone_arrives_by_event(self):
		r = self.rig()
		before = r.stats()['requests']
		r.mock('POST', 'set', {'iso': 800, 'tint': 7})
		self.assertTrue(wait(lambda: (r.state()['iso'], r.state()['tint']) == (800, 7)))
		self.assertEqual(r.stats()['requests'], before)

	def test_a_value_set_from_here_is_sent_and_confirmed(self):
		r = self.rig()
		before = r.stats()
		r.manager.set_param(r.id, 'iso', 1600)
		self.assertTrue(wait(lambda: r.state()['iso'] == 1600))
		time.sleep(0.3)
		after = r.stats()
		self.assertEqual(after['writes'] - before['writes'], 1)
		self.assertLessEqual(after['gets'] - before['gets'], 2)   # auto exposure checked, the value read back

	def test_rec_and_stop_follow_the_camera(self):
		r = self.rig()
		before = r.stats()['requests']
		r.manager.record([r.id], True)
		self.assertTrue(wait(lambda: r.state()['recording'] and not r.state()['recordPending']))
		r.manager.record([r.id], False)
		self.assertTrue(wait(lambda: not r.state()['recording'] and not r.state()['recordPending']))
		time.sleep(0.5)
		self.assertIsNone(r.state()['stopAlert'])
		st = r.stats()
		self.assertEqual(st['requests'] - before, 2, 'a take needs REC and STOP, nothing else: {}'.format(st['last'][-6:]))
		self.assertEqual(st['whileRecording'], 0)

	def test_a_held_value_is_put_back_during_a_take(self):
		r = self.rig()
		r.manager.set_param(r.id, 'wb', 4300)
		self.assertTrue(wait(lambda: r.state()['wbHold'].get('wb') == 4300))
		r.manager.record([r.id], True)
		self.assertTrue(wait(lambda: r.state()['recording'] and not r.state()['recordPending']))
		r.mock('POST', 'set', {'wb': 7000})
		self.assertTrue(wait(lambda: r.cam.state['wb'] == 4300), r.log())
		self.assertTrue(r.state()['recording'])
		self.assertEqual(r.stats()['withoutListener'], 0)

	def test_quiet_takes_send_nothing_during_a_take(self):
		r = self.rig(HEALTH_INTERVAL=0.2, QUIET_DURING_TAKE=True)
		r.manager.set_param(r.id, 'wb', 4300)   # held from here on
		self.assertTrue(wait(lambda: r.state()['wbHold'].get('wb') == 4300))
		r.manager.record([r.id], True)
		self.assertTrue(wait(lambda: r.state()['recording'] and not r.state()['recordPending']))
		r.mock('POST', 'set', {'wb': 7000})     # the phone drifts during the take
		self.assertTrue(wait(lambda: r.state()['wb'] == 7000))
		time.sleep(1.5)
		self.assertEqual(r.stats()['whileRecording'], 0, 'a request reached a recording camera: {}'.format(r.stats()['last'][-6:]))
		r.manager.record([r.id], False)
		self.assertTrue(wait(lambda: r.state()['wb'] == 4300), 'the held white balance was not put back after the take')
		self.assertEqual(r.stats()['whileRecording'], 0)

	def test_a_camera_that_stops_by_itself_raises_the_alarm(self):
		r = self.rig()
		r.manager.record([r.id], True)
		self.assertTrue(wait(lambda: r.state()['recording'] and not r.state()['recordPending']))
		time.sleep(0.5)
		r.mock('POST', 'stop')
		self.assertTrue(wait(lambda: r.state()['stopAlert'] is not None))
		self.assertEqual(r.state()['stopAlert']['kind'], 'stopped')
		self.assertFalse(r.state()['recording'])
		self.assertIn('ALERT', r.log())

	def test_a_stop_within_the_grace_after_rec_is_not_missed(self):
		r = self.rig(REC_GRACE=1.0)
		r.manager.record([r.id], True)
		self.assertTrue(wait(lambda: r.state()['recording']))
		r.mock('POST', 'stop')
		self.assertTrue(wait(lambda: r.state()['stopAlert'] is not None, 4))

	def test_a_stop_the_camera_does_not_carry_out_is_reported(self):
		r = self.rig()
		r.manager.record([r.id], True)
		self.assertTrue(wait(lambda: r.state()['recording'] and not r.state()['recordPending']))
		r.mock('POST', 'deaf', {'on': True})
		r.manager.record([r.id], False)
		self.assertTrue(wait(lambda: 'NOT CARRIED OUT' in (r.state()['error'] or '')), r.log())
		self.assertTrue(r.state()['recording'])

	def test_a_recording_camera_that_goes_away_raises_the_alarm(self):
		r = self.rig()
		r.manager.record([r.id], True)
		self.assertTrue(wait(lambda: r.state()['recording'] and not r.state()['recordPending']))
		r.server.shutdown()
		r.server.server_close()
		r.cam.events = False
		for l in list(r.cam.listeners):
			l.send(0x8)
		self.assertTrue(wait(lambda: (r.state()['stopAlert'] or {}).get('kind') == 'lost', 8), r.log())
		self.assertFalse(r.state()['online'])

	def test_a_held_white_balance_is_put_back(self):
		r = self.rig()
		r.manager.set_param(r.id, 'wb', 3200)
		self.assertTrue(wait(lambda: r.state()['wbHold'].get('wb') == 3200))
		r.mock('POST', 'set', {'wb': 6500})
		self.assertTrue(wait(lambda: r.cam.state['wb'] == 3200), r.log())
		self.assertTrue(wait(lambda: 'restored' in r.log()), r.log())

	def test_a_locked_focus_is_put_back(self):
		r = self.rig()
		r.manager.set_param(r.id, 'focuslock', True)
		self.assertTrue(wait(lambda: r.state()['focusLocked']))
		r.mock('POST', 'set', {'focus': 0.9})
		self.assertTrue(wait(lambda: abs(r.cam.state['focus'] - 0.5) < 0.01), r.log())

	def test_an_unarmed_camera_is_watched_like_any_other(self):
		r = self.rig(HEALTH_INTERVAL=0.3)
		r.manager.set_param(r.id, 'wb', 3200)
		self.assertTrue(wait(lambda: r.state()['wbHold'].get('wb') == 3200))
		r.manager.update_camera(r.id, active=False)
		before = r.stats()['requests']
		r.mock('POST', 'set', {'wb': 6500})
		self.assertTrue(wait(lambda: r.cam.state['wb'] == 3200), 'the hold is not kept on an unarmed camera')
		time.sleep(1.0)
		self.assertGreater(r.stats()['requests'], before + 2)   # its answers are still timed
		self.assertEqual(r.stats()['withoutListener'], 0)

	def test_the_health_check_is_one_small_read(self):
		r = self.rig(HEALTH_INTERVAL=0.5)
		before = r.stats()
		time.sleep(2.2)
		after = r.stats()
		sent = after['requests'] - before['requests']
		self.assertTrue(3 <= sent <= 6, sent)
		self.assertEqual(after['writes'], before['writes'])
		self.assertEqual(after['withoutListener'], 0)

	def test_slow_answers_raise_the_alarm(self):
		r = self.rig(HEALTH_INTERVAL=0.2)
		self.assertEqual(r.state()['health'], 'ok')
		r.mock('POST', 'delay', {'ms': 300})
		self.assertTrue(wait(lambda: r.state()['health'] == 'critical', 8), r.state())
		self.assertIn('APP SLOW', r.log())
		self.assertGreaterEqual(r.state()['delayMs'], 250)

	def test_a_dropped_event_channel_is_reopened_before_any_polling(self):
		r = self.rig()
		r.mock('POST', 'drop')
		self.assertTrue(wait(lambda: r.state()['gaps'] == 1 and r.state()['events'] == 'listening'), r.log())
		time.sleep(0.5)
		st = r.stats()
		self.assertEqual(st['listeners'], 1)
		self.assertEqual(st['withoutListener'], 0, 'a request was sent while the event channel was down')
		self.assertTrue(r.state()['online'])

	def test_a_camera_without_an_event_channel_is_polled_and_says_so(self):
		r = self.rig(events=False)
		s = r.state()
		self.assertEqual((s['events'], s['polled']), ('none', True))
		self.assertEqual((s['iso'], s['letter']), (400, 'A'))
		before = r.stats()['requests']
		time.sleep(2.0)
		self.assertGreater(r.stats()['requests'] - before, 8)
		r.mock('POST', 'set', {'iso': 800})                 # a change on the camera is picked up
		self.assertTrue(wait(lambda: r.state()['iso'] == 800))
		time.sleep(1.5)
		self.assertIsNone(r.state()['pollAlert'])           # it has no channel: a label, not an alarm
		r.manager.update_camera(r.id, active=False)         # unarmed: polled all the same
		before = r.stats()['requests']
		time.sleep(1.5)
		self.assertGreater(r.stats()['requests'] - before, 5)

	def test_a_phone_whose_channel_will_not_open_is_polled_and_raises_the_alarm(self):
		# what happened with the Python 3.13 defect: the phone has the channel, the controller
		# cannot use it, and polls with nothing listening
		r = self.rig(ws_broken=True)
		self.assertTrue(wait(lambda: r.state()['polled']))
		self.assertEqual(r.stats()['listeners'], 0)
		self.assertTrue(wait(lambda: r.state()['pollAlert'] is not None, 5), r.log())
		self.assertIn('POLLED WITH NOTHING LISTENING', r.log())
		r.cam.ws_broken = False                             # the channel can be opened again
		self.assertTrue(wait(lambda: r.state()['events'] == 'listening' and not r.state()['polled'], 8), r.log())
		self.assertTrue(wait(lambda: r.state()['pollAlert'] is None))
		time.sleep(0.5)
		before = r.stats()['requests']
		time.sleep(1.5)
		self.assertEqual(r.stats()['requests'], before, 'still polled with the channel open')

	def test_a_channel_that_stays_down_falls_back_to_polling_with_the_alarm(self):
		r = self.rig()
		self.assertFalse(r.state()['polled'])
		r.cam.ws_broken = True
		r.mock('POST', 'drop')
		self.assertTrue(wait(lambda: r.state()['polled']), r.log())
		r.mock('POST', 'set', {'iso': 800})                 # the panel stays live by polling
		self.assertTrue(wait(lambda: r.state()['iso'] == 800))
		self.assertTrue(r.state()['online'])
		self.assertTrue(wait(lambda: r.state()['pollAlert'] is not None, 5), r.log())
		r.cam.ws_broken = False
		self.assertTrue(wait(lambda: r.state()['events'] == 'listening' and r.state()['pollAlert'] is None, 8), r.log())
		self.assertGreaterEqual(r.state()['gaps'], 1)

	def test_quiet_takes_leave_a_camera_found_recording_alone(self):
		server, cam = mock_camera.start(0, 'Rolling', host='127.0.0.1')
		self.addCleanup(server.shutdown)
		cam.set_recording(True)
		settings = dict(FAST, QUIET_DURING_TAKE=True)
		saved = {k: getattr(CameraWorker, k) for k in settings}
		for k, v in settings.items():
			setattr(CameraWorker, k, v)
		self.addCleanup(lambda: [setattr(CameraWorker, k, v) for k, v in saved.items()])
		manager = CameraManager(config_path=None, autosave=False)
		manager.notify = False
		self.addCleanup(manager.shutdown)
		cam_id = manager.add_camera('http://127.0.0.1:{}'.format(server.server_address[1]))
		state = lambda: manager._worker(cam_id).snapshot()
		self.assertTrue(wait(lambda: state()['online'] and state()['recording']))
		time.sleep(1.0)
		self.assertLessEqual(cam.stats['whileRecording'], 1)   # the one read that told us it records
		self.assertEqual(state()['iso'], 400)                  # what its events carry is there already
		self.assertEqual(state()['displayLuts'], {})           # what they do not carry waits
		manager.record([cam_id], False)
		self.assertTrue(wait(lambda: 'HDMI' in state()['displayLuts']), 'the state was not read when the take ended')
		self.assertIsNone(state()['stopAlert'])
		self.assertLessEqual(cam.stats['whileRecording'], 1)


	def test_no_thread_class_hides_one_of_pythons_own_thread_attributes(self):
		# A method named like an attribute threading.Thread sets on itself is replaced by it
		# when the thread starts (Python 3.13 added _handle), and then fails when called.
		import threading
		from lucam import events
		t = threading.Thread(target=lambda: None)
		t.start()
		t.join()
		for cls in (events.EventChannel, CameraWorker):
			clash = sorted(n for n in vars(cls) if n in vars(t))
			self.assertEqual(clash, [], '{} defines {}'.format(cls.__name__, clash))


if __name__ == '__main__':
	unittest.main(verbosity=2)
