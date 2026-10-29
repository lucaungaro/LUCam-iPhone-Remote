"""Camera discovery: Bonjour (mDNS/DNS-SD) browsing and a subnet scan fallback.

The mDNS client is a minimal one-shot querier (RFC 6762 section 5.1): it sends
queries from an ephemeral port and responders answer it directly, so it works
next to the system's own mDNS daemon and needs no third-party package.
Every candidate is then probed on the camera REST API, so only devices that
actually answer /control/api/v1 are reported.
"""

import ipaddress
import select
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor

from . import api

MDNS_ADDR = ('224.0.0.251', 5353)

# Service types the camera's web server may advertise. Blackmagic cameras
# publish their Web Media Manager over HTTP; the extra types cost nothing.
SERVICE_TYPES = (
	'_http._tcp.local',
	'_https._tcp.local',
	'_blackmagic._tcp.local',
)

T_A, T_PTR, T_TXT, T_SRV = 1, 12, 16, 33


def _encode_name(name):
	out = b''
	for label in name.strip('.').split('.'):
		raw = label.encode('utf-8')
		out += bytes([len(raw)]) + raw
	return out + b'\0'


def _build_query(questions):
	"""questions: list of (name, qtype). The QU bit asks for unicast replies."""
	pkt = struct.pack('>HHHHHH', 0, 0, len(questions), 0, 0, 0)
	for name, qtype in questions:
		pkt += _encode_name(name) + struct.pack('>HH', qtype, 0x8001)
	return pkt


def _read_name(data, off):
	labels = []
	end = None
	hops = 0
	while True:
		length = data[off]
		if length == 0:
			off += 1
			break
		if length & 0xC0 == 0xC0:
			if end is None:
				end = off + 2
			off = ((length & 0x3F) << 8) | data[off + 1]
			hops += 1
			if hops > 64:
				raise ValueError('mDNS name pointer loop')
			continue
		labels.append(data[off + 1:off + 1 + length].decode('utf-8', 'replace'))
		off += 1 + length
	return '.'.join(labels), (end if end is not None else off)


def _parse_packet(data):
	"""Yield (name, type, value) for every resource record in the packet."""
	_, flags, qd, an, ns, ar = struct.unpack('>HHHHHH', data[:12])
	if not flags & 0x8000:  # a query, not a response
		return
	off = 12
	for _ in range(qd):
		_, off = _read_name(data, off)
		off += 4
	for _ in range(an + ns + ar):
		name, off = _read_name(data, off)
		rtype, _, _, rdlen = struct.unpack('>HHIH', data[off:off + 10])
		off += 10
		rdata_off = off
		off += rdlen
		if rtype == T_PTR:
			yield name, rtype, _read_name(data, rdata_off)[0]
		elif rtype == T_SRV:
			_, _, port = struct.unpack('>HHH', data[rdata_off:rdata_off + 6])
			yield name, rtype, (_read_name(data, rdata_off + 6)[0], port)
		elif rtype == T_A and rdlen == 4:
			yield name, rtype, socket.inet_ntoa(data[rdata_off:rdata_off + 4])
		elif rtype == T_TXT:
			txt = {}
			p = rdata_off
			while p < rdata_off + rdlen:
				ln = data[p]
				k, _, v = data[p + 1:p + 1 + ln].decode('utf-8', 'replace').partition('=')
				if k:
					txt[k.lower()] = v
				p += 1 + ln
			yield name, rtype, txt


def local_ipv4_addresses():
	ips = set()
	try:
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		s.connect(('192.0.2.1', 9))  # no packet is sent; just picks the default route
		ips.add(s.getsockname()[0])
		s.close()
	except OSError:
		pass
	try:
		for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
			ips.add(info[4][0])
	except OSError:
		pass
	return sorted(ip for ip in ips if not ip.startswith('127.'))


def mdns_browse(service_types=SERVICE_TYPES, timeout=2.5):
	"""Return [{'name', 'host', 'ip', 'port', 'service'}] for every instance."""
	socks = []
	for ip in local_ipv4_addresses() or ['0.0.0.0']:
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
		try:
			if ip != '0.0.0.0':
				s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
			s.bind((ip, 0))
		except OSError:
			s.close()
			continue
		socks.append(s)
	if not socks:
		return []

	ptr, srv, addr, txt = {}, {}, {}, {}

	def send(questions):
		pkt = _build_query(questions)
		for s in socks:
			try:
				s.sendto(pkt, MDNS_ADDR)
			except OSError:
				pass

	types = [t.strip('.') for t in service_types]
	send([(t, T_PTR) for t in types])
	deadline = time.monotonic() + timeout
	resent = followed_up = False
	try:
		while True:
			now = time.monotonic()
			if now >= deadline:
				break
			if not resent and now > deadline - timeout * 0.6:
				send([(t, T_PTR) for t in types])
				resent = True
			if not followed_up and now > deadline - timeout * 0.35:
				# Ask directly for SRV/A records responders left out.
				qs = [(inst, T_SRV) for insts in ptr.values() for inst in insts if inst not in srv]
				qs += [(target, T_A) for target, _ in srv.values() if target not in addr]
				if qs:
					send(qs[:40])
				followed_up = True
			ready, _, _ = select.select(socks, [], [], min(0.2, deadline - now))
			for s in ready:
				try:
					data, _ = s.recvfrom(9000)
					for name, rtype, value in _parse_packet(data):
						if rtype == T_PTR:
							ptr.setdefault(name.lower(), set()).add(value)
						elif rtype == T_SRV:
							srv[value_key(name)] = value
						elif rtype == T_A:
							addr[name.lower()] = value
						elif rtype == T_TXT:
							txt[value_key(name)] = value
				except (OSError, ValueError, struct.error, IndexError):
					continue
	finally:
		for s in socks:
			s.close()

	results = []
	for stype in types:
		for inst in ptr.get(stype.lower(), ()):
			target, port = srv.get(value_key(inst), (None, None))
			if not target:
				continue
			label = inst[:-(len(stype) + 1)] if inst.lower().endswith(stype.lower()) else inst
			results.append({
				'name': label,
				'host': target.rstrip('.'),
				'ip': addr.get(target.lower()),
				'port': port,
				'service': stype,
				'txt': txt.get(value_key(inst), {}),
			})
	return results


def value_key(name):
	return name.lower().rstrip('.')


def discover_bonjour(timeout=2.5, probe_timeout=1.5):
	"""Browse Bonjour and keep only instances that answer the camera API."""
	found = {}
	for inst in mdns_browse(timeout=timeout):
		host = inst['ip'] or inst['host']
		found.setdefault((host, inst['port']), inst)

	def check(item):
		(host, port), inst = item
		# The iPhone app advertises _http._tcp but only speaks HTTPS, so try both.
		schemes = ('http', 'https') if inst['service'].startswith('_https') else ('https', 'http')
		for scheme in schemes:
			address = api.format_address(scheme, host, port)
			info = api.probe(address, timeout=probe_timeout)
			if info is not None:
				break
		else:
			return None
		return {
			'address': address,
			'hostname': inst['host'],
			'name': info.get('deviceName') or inst['name'],
			'product': info.get('productName', ''),
			'source': 'bonjour',
		}

	with ThreadPoolExecutor(max_workers=16) as pool:
		return [r for r in pool.map(check, found.items()) if r]


def discover_subnet(ports=(api.APP_PORT, 80), connect_timeout=0.35, probe_timeout=1.5):
	"""Probe every host of each local /24 for the camera REST API."""
	hosts = set()
	for ip in local_ipv4_addresses():
		net = ipaddress.ip_network(ip + '/24', strict=False)
		hosts.update(str(h) for h in net.hosts())

	def port_open(target):
		host, port = target
		try:
			with socket.create_connection((host, port), timeout=connect_timeout):
				return target
		except OSError:
			return None

	targets = [(h, p) for h in sorted(hosts) for p in ports]
	with ThreadPoolExecutor(max_workers=128) as pool:
		open_targets = [t for t in pool.map(port_open, targets) if t]

	def check(target):
		host, port = target
		for scheme in (('https', 'http') if port in (api.APP_PORT, 443) else ('http', 'https')):
			address = api.format_address(scheme, host, port)
			info = api.probe(address, timeout=probe_timeout)
			if info is not None:
				break
		else:
			return None
		return {
			'address': address,
			'hostname': '',
			'name': info.get('deviceName') or host,
			'product': info.get('productName', ''),
			'source': 'scan',
		}

	with ThreadPoolExecutor(max_workers=32) as pool:
		return [r for r in pool.map(check, open_targets) if r]
