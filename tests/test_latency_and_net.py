import ipaddress

import pytest
from pyatv.protocols.raop.protocols import StreamContext

from airpy import airplay, net


@pytest.fixture
def restore_latency():
    saved = airplay._latency_frames, airplay._read_delay
    yield
    airplay._latency_frames, airplay._read_delay = saved


def test_latency_is_clamped(restore_latency):
    airplay.set_latency_ms(1)
    assert airplay._latency_frames == int(airplay.RATE * airplay.LATENCY_MIN_MS / 1000)
    airplay.set_latency_ms(10**6)
    assert airplay._latency_frames == int(airplay.RATE * airplay.LATENCY_MAX_MS / 1000)


def test_total_latency_adds_buffer_read_margin_and_capture_block():
    assert airplay.total_latency_ms(200) == 200 + 40 + 15   # narrow read margin
    assert airplay.total_latency_ms(1500) == 1500 + 100 + 15  # wide read margin
    assert airplay.total_latency_ms(10) == airplay.total_latency_ms(airplay.LATENCY_MIN_MS)


def test_stream_context_uses_the_selected_buffer(restore_latency):
    airplay.set_latency_ms(300)
    context = StreamContext()
    context.reset()
    assert context.latency == int(airplay.RATE * 0.3)


@pytest.fixture
def two_cards(monkeypatch):
    cards = [ipaddress.IPv4Interface("192.168.1.14/24"), ipaddress.IPv4Interface("10.0.0.5/24")]
    monkeypatch.setattr(net, "lan_interfaces", lambda max_age=10.0: cards)
    monkeypatch.setattr(net, "_working_source", {})
    return cards


def test_source_is_the_card_on_the_targets_subnet(two_cards):
    assert net.sources_for("192.168.1.17") == ["192.168.1.14"]
    assert net.sources_for("10.0.0.9") == ["10.0.0.5"]
    assert net.sources_for("8.8.8.8") == []
    assert net.sources_for("not-an-ip") == []


def test_known_working_source_is_tried_first(monkeypatch):
    cards = [ipaddress.IPv4Interface("192.168.1.14/24"), ipaddress.IPv4Interface("192.168.1.7/24")]
    monkeypatch.setattr(net, "lan_interfaces", lambda max_age=10.0: cards)
    monkeypatch.setattr(net, "_working_source", {"192.168.1.17": "192.168.1.7"})
    assert net.sources_for("192.168.1.17") == ["192.168.1.7", "192.168.1.14"]


def test_primary_lan_ip_prefers_the_targets_subnet(two_cards):
    assert net.primary_lan_ip(near="10.0.0.9") == "10.0.0.5"
    assert net.primary_lan_ip() == "192.168.1.14"
