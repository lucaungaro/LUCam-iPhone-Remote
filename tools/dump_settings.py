#!/usr/bin/env python3
"""Dump every camera's settings and presets to files.

The Blackmagic Camera app's presets are JSON files holding the app's whole
configuration. Saving the current settings as a preset and downloading it gives
a snapshot of the phone exactly as it is now, without touching the phone's
screen; the phone's own presets are downloaded too.

    python3 tools/dump_settings.py                      # cameras from cameras.json
    python3 tools/dump_settings.py 192.168.1.20 cam-k.local --out dumps/
    python3 tools/dump_settings.py --server http://localhost:9980   # from a running LUCam

For each camera a folder <out>/<name>/ receives:
    snapshot-YYYYMMDD-HHMMSS.bmcpreset   the settings as they are now
    <preset files>                       every preset stored on the phone
and <out>/compare.txt lists the settings that differ between the cameras.

The snapshot preset is deleted from the phone afterwards (--keep leaves it).
Nothing else on the phone is changed.
"""

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'python'))
from lucam import api, events  # noqa: E402


def cameras_from_config(path):
	with open(path) as f:
		cfg = json.load(f)
	return [(c.get('name') or c['address'], c['address']) for c in cfg.get('cameras', [])]


def cameras_from_server(url):
	with urllib.request.urlopen(url.rstrip('/') + '/api/state', timeout=5) as r:
		state = json.load(r)
	return [(c.get('name') or c.get('address'), c['address']) for c in state['cameras']]


def flatten(obj, prefix=''):
	out = {}
	if isinstance(obj, dict):
		for k, v in obj.items():
			out.update(flatten(v, prefix + k + '.'))
	elif isinstance(obj, list):
		out[prefix.rstrip('.')] = json.dumps(obj)
	else:
		out[prefix.rstrip('.')] = obj
	return out


def safe(name):
	return ''.join(ch if ch.isalnum() or ch in '-_. ' else '_' for ch in name).strip() or 'camera'


def dump(name, address, out, keep):
	client = api.CameraClient(address, timeout=5.0)
	folder = os.path.join(out, safe(name))
	os.makedirs(folder, exist_ok=True)
	report = {'name': name, 'address': address, 'presets': [], 'snapshot': None, 'error': None}
	# The phone's event channel is held open for the duration: the app must not be
	# sent requests while nothing listens to it (see python/lucam/events.py).
	with events.listening(address) as report['listening']:
		_dump(client, folder, report, keep)
	return report


def _dump(client, folder, report, keep):
	try:
		existing = client.presets()
		for p in existing:
			data, filename = client.download_preset(p)
			with open(os.path.join(folder, safe(filename)), 'wb') as f:
				f.write(data)
			report['presets'].append(filename)
		tag = time.strftime('snapshot-%Y%m%d-%H%M%S')
		client.save_preset(tag)
		try:
			names = client.presets()
			made = next((n for n in names if n.startswith(tag)), tag)
			data, filename = client.download_preset(made)
			path = os.path.join(folder, safe(filename))
			with open(path, 'wb') as f:
				f.write(data)
			report['snapshot'] = path
			report['settings'] = json.loads(data)
		finally:
			if not keep:
				try:
					client.delete_preset(made)
				except api.ApiError:
					pass
	except (api.ApiError, OSError, ValueError) as e:
		report['error'] = str(e)


def main():
	ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
	ap.add_argument('addresses', nargs='*', help='camera addresses (default: the cameras in cameras.json)')
	ap.add_argument('--config', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cameras.json'))
	ap.add_argument('--server', help='running LUCam, e.g. http://localhost:9980, to take the camera list from')
	ap.add_argument('--out', default=os.path.join('settings-dumps', time.strftime('%Y%m%d-%H%M%S')))
	ap.add_argument('--keep', action='store_true', help='leave the snapshot preset on the phones')
	args = ap.parse_args()

	if args.addresses:
		cams = [(a, a) for a in args.addresses]
	elif args.server:
		cams = cameras_from_server(args.server)
	else:
		cams = cameras_from_config(args.config)
	if not cams:
		sys.exit('no cameras')
	os.makedirs(args.out, exist_ok=True)

	reports = []
	for name, address in cams:
		print('{:<12} {:<28}'.format(name, address), end=' ', flush=True)
		r = dump(name, address, args.out, args.keep)
		reports.append(r)
		if r['error']:
			print('FAILED: ' + r['error'])
		else:
			print('snapshot ok, {} preset(s) on the phone{}'.format(
				len(r['presets']), '' if r['listening'] else ' (no event channel on this camera)'))

	good = [r for r in reports if r.get('settings')]
	flat = {r['name']: flatten(r['settings']) for r in good}
	keys = sorted(set().union(*flat.values())) if flat else []
	lines = ['Settings dumped {}'.format(time.strftime('%Y-%m-%d %H:%M:%S')), '']
	lines.append('Cameras: ' + ', '.join(r['name'] for r in good))
	if len(good) > 1:
		lines += ['', 'Settings that differ between cameras:', '']
		width = max(len(n) for n in flat)
		for k in keys:
			vals = {n: flat[n].get(k, '<absent>') for n in flat}
			if len({json.dumps(v) for v in vals.values()}) > 1:
				lines.append(k)
				for n in flat:
					lines.append('    {:<{w}}  {}'.format(n, json.dumps(vals[n]), w=width))
	lines += ['', 'All settings (first camera, {}):'.format(good[0]['name']) if good else '']
	if good:
		for k in keys:
			lines.append('    {} = {}'.format(k, json.dumps(flat[good[0]['name']].get(k))))
	with open(os.path.join(args.out, 'compare.txt'), 'w') as f:
		f.write('\n'.join(lines) + '\n')
	print('\nwritten to {}  (compare.txt lists the settings that differ)'.format(os.path.abspath(args.out)))


if __name__ == '__main__':
	main()
