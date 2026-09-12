#!/usr/bin/env python3
"""Correlate CycloneDDS packet capture DATA/DATA_FRAG with discovered writers."""
import argparse
import collections
import ipaddress
import json
from pathlib import Path
import struct


DATA_KINDS = {0x15: 'DATA', 0x16: 'DATA_FRAG'}


def rtps_user_data(payload):
    """Return user DATA submessages as (kind, writer GUID) without decoding data."""
    if len(payload) < 20 or payload[:4] != b'RTPS':
        return []
    prefix = payload[8:20].hex()
    result = []
    offset = 20
    while offset + 4 <= len(payload):
        kind, flags = payload[offset], payload[offset + 1]
        endian = '<' if flags & 1 else '>'
        length = struct.unpack_from(endian + 'H', payload, offset + 2)[0]
        body_start = offset + 4
        body_end = len(payload) if length == 0 else body_start + length
        if body_end > len(payload):
            break
        if kind in DATA_KINDS and body_end - body_start >= 12:
            writer_id = payload[body_start + 8:body_start + 12].hex()
            result.append((DATA_KINDS[kind], prefix + writer_id))
        if length == 0:
            break
        offset = body_end
    return result


def ipv4_udp(packet, linktype):
    if linktype == 1:
        offset = 14
        if len(packet) < offset or packet[12:14] != b'\x08\x00':
            return None
    elif linktype == 101:
        offset = 0
    elif linktype == 113:
        offset = 16
        if len(packet) < offset or packet[14:16] != b'\x08\x00':
            return None
    elif linktype == 276:
        offset = 20
        if len(packet) < offset or packet[:2] != b'\x08\x00':
            return None
    else:
        return None
    if len(packet) < offset + 20 or packet[offset] >> 4 != 4:
        return None
    ihl = (packet[offset] & 15) * 4
    if ihl < 20 or len(packet) < offset + ihl + 8 or packet[offset + 9] != 17:
        return None
    udp = offset + ihl
    return {'ttl': packet[offset + 8],
            'source_ip': str(ipaddress.IPv4Address(packet[offset + 12:offset + 16])),
            'destination_ip': str(ipaddress.IPv4Address(packet[offset + 16:offset + 20])),
            'source_port': struct.unpack_from('!H', packet, udp)[0],
            'destination_port': struct.unpack_from('!H', packet, udp + 2)[0],
            'payload': packet[udp + 8:]}


def read_pcap(path):
    with path.open('rb') as stream:
        header = stream.read(24)
        if len(header) != 24:
            raise ValueError('truncated pcap header')
        magic = header[:4]
        formats = {b'\xd4\xc3\xb2\xa1': ('<', 1_000),
                   b'\xa1\xb2\xc3\xd4': ('>', 1_000),
                   b'\x4d\x3c\xb2\xa1': ('<', 1),
                   b'\xa1\xb2\x3c\x4d': ('>', 1)}
        if magic not in formats:
            raise ValueError('unsupported pcap magic')
        endian, fraction_to_ns = formats[magic]
        linktype = struct.unpack_from(endian + 'I', header, 20)[0]
        while True:
            record = stream.read(16)
            if not record:
                return
            if len(record) != 16:
                raise ValueError('truncated pcap record header')
            sec, fraction, captured, original = struct.unpack(endian + 'IIII', record)
            packet = stream.read(captured)
            if len(packet) != captured:
                raise ValueError('truncated pcap packet')
            yield sec * 1_000_000_000 + fraction * fraction_to_ns, linktype, original, packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('telemetry_jsonl', type=Path)
    parser.add_argument('pcap', type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.telemetry_jsonl.read_text().splitlines()]
    summary = next(row for row in rows if row.get('event') == 'summary')
    writer_topics = {}
    for topic, item in summary['topics'].items():
        for endpoint in item['writer_endpoints']:
            writer_topics[endpoint.replace('-', '')] = topic
    counts = {topic: {'DATA': 0, 'DATA_FRAG': 0, 'packets': 0,
                      'first_data_unix_ns': None, 'last_data_unix_ns': None,
                      'source_ips': set(), 'wire_bytes': 0}
              for topic in summary['topics']}
    packets_total = rtps_packets = received_packets = sent_packets = 0
    all_user_data = {'DATA': 0, 'DATA_FRAG': 0}
    writers_with_user_data = set()
    prefix_source_ips = collections.defaultdict(set)
    for timestamp, linktype, original, packet in read_pcap(args.pcap):
        packets_total += 1
        udp = ipv4_udp(packet, linktype)
        if udp is None:
            continue
        submessages = rtps_user_data(udp['payload'])
        if udp['payload'][:4] == b'RTPS':
            rtps_packets += 1
            received_packets += udp['ttl'] == 128
            sent_packets += udp['ttl'] == 255
            if udp['ttl'] == 128 and len(udp['payload']) >= 20:
                prefix_source_ips[udp['payload'][8:20].hex()].add(udp['source_ip'])
        touched = set()
        for kind, writer in submessages:
            all_user_data[kind] += 1
            writers_with_user_data.add(writer)
            topic = writer_topics.get(writer)
            if topic is None:
                continue
            item = counts[topic]
            item[kind] += 1
            item['first_data_unix_ns'] = item['first_data_unix_ns'] or timestamp
            item['last_data_unix_ns'] = timestamp
            item['source_ips'].add(udp['source_ip'])
            if topic not in touched:
                item['packets'] += 1
                item['wire_bytes'] += original
                touched.add(topic)
    for topic, item in counts.items():
        item['source_ips'] = sorted(item['source_ips'])
        item['participant_source_ips'] = sorted({source
            for writer in summary['topics'][topic]['writer_endpoints']
            for source in prefix_source_ips[writer.replace('-', '')[:24]]})
        match = summary['topics'][topic]['first_match_unix_ns']
        item['first_data_after_match_s'] = (
            (item['first_data_unix_ns'] - match) / 1e9
            if item['first_data_unix_ns'] is not None and match is not None else None)
    print(json.dumps({'pcap_packets': packets_total, 'rtps_packets': rtps_packets,
                      'received_rtps_packets': received_packets,
                      'sent_rtps_packets': sent_packets,
                      'all_user_data_submessages': all_user_data,
                      'writers_with_user_data': len(writers_with_user_data),
                      'topics': counts}, indent=2))


if __name__ == '__main__':
    main()
