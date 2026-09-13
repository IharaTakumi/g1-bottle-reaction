#!/usr/bin/env python3
"""Bounded LowState receive-only benchmark with no application writer or RPC.

The DDS participant necessarily emits discovery and acknowledgement traffic. It
creates exactly one application DataReader for rt/lowstate and no command topic.
"""
import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import statistics
import threading
import time
import xml.etree.ElementTree as ET


def config_for_interface(base_xml, interface, peers=()):
    root = ET.fromstring(base_xml)
    nodes = root.findall('./Domain/General/Interfaces/NetworkInterface')
    if len(nodes) != 1:
        raise ValueError('expected exactly one CycloneDDS NetworkInterface')
    nodes[0].set('name', interface)
    if peers:
        domain = root.find('./Domain')
        discovery = domain.find('Discovery')
        if discovery is None:
            discovery = ET.SubElement(domain, 'Discovery')
        peers_node = discovery.find('Peers')
        if peers_node is None:
            peers_node = ET.SubElement(discovery, 'Peers')
        for peer in peers:
            ET.SubElement(peers_node, 'Peer', Address=str(peer))
    return ET.tostring(root, encoding='unicode')


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default=os.environ.get('G1_NETWORK_INTERFACE', 'enp129s0'))
    parser.add_argument('--peer', action='append', default=[],
                        help='Optional process-local unicast discovery peer IPv4 address')
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--stale-ms', type=float, default=100)
    args = parser.parse_args()
    if not 0 < args.seconds <= 60:
        parser.error('--seconds must be in (0, 60]')
    if not 0 < args.stale_ms <= 1000:
        parser.error('--stale-ms must be in (0, 1000]')
    socket.if_nametoindex(args.interface)
    peers = [ipaddress.IPv4Address(value) for value in args.peer]

    root = Path(__file__).resolve().parents[1]
    base_xml = (root / 'config/g1-readonly-dds.xml').read_text()
    config = config_for_interface(base_xml, args.interface, peers)
    os.environ['CYCLONEDDS_URI'] = config

    from cyclonedds.core import Listener
    from cyclonedds.domain import Domain, DomainParticipant
    from cyclonedds.internal import InvalidSample
    from cyclonedds.qos import Policy, Qos
    from cyclonedds.sub import DataReader
    from cyclonedds.topic import Topic
    from g1_bottle_reaction.adapters.g1_robot import UnitreeSdkRuntime

    lock = threading.Lock()
    arrivals_ns = []
    ticks = set()
    invalid_samples = 0
    matched_current = 0
    matched_total = 0
    sample_lost = 0
    sample_rejected = 0
    incompatible_qos = 0

    def available(reader):
        nonlocal invalid_samples
        samples = reader.take(1024)
        with lock:
            for sample in samples:
                if isinstance(sample, InvalidSample):
                    invalid_samples += 1
                    continue
                arrivals_ns.append(time.monotonic_ns())
                ticks.add(int(sample.tick))

    def matched(reader, status):
        nonlocal matched_current, matched_total
        with lock:
            matched_current = int(status.current_count)
            matched_total = int(status.total_count)

    def lost(reader, status):
        nonlocal sample_lost
        with lock:
            sample_lost = int(status.total_count)

    def rejected(reader, status):
        nonlocal sample_rejected
        with lock:
            sample_rejected = int(status.total_count)

    def incompatible(reader, status):
        nonlocal incompatible_qos
        with lock:
            incompatible_qos = int(status.total_count)

    domain = Domain(0, config)
    participant = DomainParticipant(0)
    state_type = UnitreeSdkRuntime().load_readonly_low_state_type()
    topic = Topic(participant, 'rt/lowstate', state_type)
    listener = Listener(on_data_available=available,
                        on_subscription_matched=matched,
                        on_sample_lost=lost,
                        on_sample_rejected=rejected,
                        on_requested_incompatible_qos=incompatible)
    started_ns = time.monotonic_ns()
    reader = DataReader(
        participant, topic,
        Qos(Policy.Reliability.BestEffort, Policy.Durability.Volatile,
            Policy.History.KeepLast(1024)),
        listener=listener,
    )
    print(json.dumps({'event': 'start', 'interface': args.interface, 'domain': 0,
                      'topic': 'rt/lowstate', 'seconds': args.seconds,
                      'discovery_peers': [str(value) for value in peers],
                      'application_writers': 0, 'rpc_clients': 0}), flush=True)
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    ended_ns = time.monotonic_ns()

    with lock:
        captured = list(arrivals_ns)
        tick_count = len(ticks)
        status = {
            'matched_current': matched_current,
            'matched_total': matched_total,
            'invalid_samples': invalid_samples,
            'sample_lost': sample_lost,
            'sample_rejected': sample_rejected,
            'incompatible_qos': incompatible_qos,
        }
    intervals_ms = [(b - a) / 1e6 for a, b in zip(captured, captured[1:])]
    elapsed_s = (ended_ns - started_ns) / 1e9
    stale_events = sum(value > args.stale_ms for value in intervals_ms)
    one_second_stops = sum(value > 1000 for value in intervals_ms)
    result = {
        'event': 'summary',
        'interface': args.interface,
        'domain': 0,
        'topic': 'rt/lowstate',
        'elapsed_s': elapsed_s,
        'samples': len(captured),
        'distinct_ticks': tick_count,
        'callback_rate_hz': len(captured) / elapsed_s,
        'first_sample_delay_ms': ((captured[0] - started_ns) / 1e6) if captured else None,
        'ending_gap_ms': ((ended_ns - captured[-1]) / 1e6) if captured else None,
        'interval_mean_ms': statistics.fmean(intervals_ms) if intervals_ms else None,
        'interval_median_ms': statistics.median(intervals_ms) if intervals_ms else None,
        'interval_p95_ms': percentile(intervals_ms, 0.95),
        'interval_p99_ms': percentile(intervals_ms, 0.99),
        'max_gap_ms': max(intervals_ms) if intervals_ms else None,
        'stale_threshold_ms': args.stale_ms,
        'stale_events': stale_events,
        'stops_over_1s': one_second_stops,
        **status,
    }
    print(json.dumps(result), flush=True)
    del reader, topic, participant, domain
    return 0 if len(captured) > 1 else 2


if __name__ == '__main__':
    raise SystemExit(main())
