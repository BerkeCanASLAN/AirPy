"""Local network helpers.

VPN/tunnel software (Tailscale, WARP, ...) can define a higher-priority route for the local
subnet. The connection to the speaker then leaves through the tunnel and AirPlay's UDP
timing/control channels do not work. The layer here binds the connections the asyncio loop opens
to the physical adapter on the same subnet as the target.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time

import ifaddr

_LOG = logging.getLogger(__name__)

_VIRTUAL_HINTS = (
    "tailscale",
    "warp",
    "cloudflare",
    "wireguard",
    "zerotier",
    "vpn",
    "tap-",
    "tunnel",
    "loopback",
    "vethernet",
    "hyper-v",
    "virtualbox",
    "vmware",
    "docker",
    "wsl",
    "bluetooth",
    "npcap",
)

_cache: tuple[float, list[ipaddress.IPv4Interface]] = (0.0, [])
_working_source: dict[str, str] = {}  # target IP -> source IP known to work


def _usable(ip: str) -> bool:
    """Is the address really usable right now (can a socket bind to it)?"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((ip, 0))
        return True
    except OSError:
        return False


def lan_interfaces(max_age: float = 10.0) -> list[ipaddress.IPv4Interface]:
    """IPv4 addresses of the physical adapters (wired ones first)."""
    global _cache
    stamp, cached = _cache
    if cached and time.monotonic() - stamp < max_age:
        return cached

    found: list[tuple[int, ipaddress.IPv4Interface]] = []
    for adapter in ifaddr.get_adapters():
        label = f"{adapter.nice_name} {adapter.name}".lower()
        if any(hint in label for hint in _VIRTUAL_HINTS):
            continue
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):  # IPv6
                continue
            try:
                iface = ipaddress.IPv4Interface(f"{ip.ip}/{ip.network_prefix}")
            except ValueError:
                continue
            if iface.ip.is_loopback or iface.ip.is_link_local or not iface.ip.is_private:
                continue
            if not _usable(str(iface.ip)):  # an address left on a disconnected adapter
                continue
            wireless = any(w in label for w in ("wi-fi", "wifi", "wireless", "wlan"))
            found.append((1 if wireless else 0, iface))
    found.sort(key=lambda item: item[0])
    result = [iface for _, iface in found]
    _cache = (time.monotonic(), result)
    return result


_TAILNET = ipaddress.IPv4Network("100.64.0.0/10")  # the CGNAT range Tailscale uses


def tailscale_ip() -> str:
    """This PC's Tailscale address (empty string if Tailscale is off or not installed)."""
    try:
        for adapter in ifaddr.get_adapters():
            if "tailscale" not in f"{adapter.nice_name} {adapter.name}".lower():
                continue
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and ipaddress.IPv4Address(ip.ip) in _TAILNET:
                    if _usable(ip.ip):
                        return ip.ip
    except Exception as exc:
        _LOG.debug("Could not read the Tailscale address: %r", exc)
    return ""


def sources_for(host: str) -> list[str]:
    """Local source addresses on the same subnet as ``host``."""
    try:
        target = ipaddress.IPv4Address(host)
    except ValueError:
        return []
    candidates = [str(i.ip) for i in lan_interfaces() if target in i.network]
    known = _working_source.get(host)
    if known in candidates:
        candidates.remove(known)
        candidates.insert(0, known)
    return candidates


def primary_lan_ip(near: str | None = None) -> str:
    """Local IP the phone can reach (for the QR code)."""
    if near:
        for source in sources_for(near):
            return source
    interfaces = lan_interfaces()
    return str(interfaces[0].ip) if interfaces else "127.0.0.1"


class MdnsName:
    """Publishes a fixed name (e.g. ``airpy.local``) on the local network.

    The shortcut/bookmark on the phone keeps working with the same address even if the PC's IP
    changes. It is announced from physical adapters only; if it fails it is skipped silently.
    """

    def __init__(self, label: str = "airpy") -> None:
        self._label = label
        self._zeroconf = None
        self._ips: tuple[str, ...] = ()
        self._port = 0

    @property
    def host(self) -> str:
        return f"{self._label}.local" if self._zeroconf is not None else ""

    async def start(self, port: int) -> None:
        ips = tuple(str(i.ip) for i in lan_interfaces(max_age=0))
        if not ips or self._zeroconf is not None:
            return
        try:
            from zeroconf import IPVersion
            from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

            zeroconf = AsyncZeroconf(interfaces=list(ips), ip_version=IPVersion.V4Only)
            info = AsyncServiceInfo(
                "_http._tcp.local.",
                f"AirPy ({socket.gethostname()})._http._tcp.local.",
                addresses=[socket.inet_aton(ips[0])],  # preferred (wired) adapter
                port=port,
                server=f"{self._label}.local.",
            )
            await zeroconf.async_register_service(info, allow_name_change=True)
        except Exception as exc:
            _LOG.warning("Could not publish the local name (%s.local): %r", self._label, exc)
            return
        self._zeroconf, self._ips, self._port = zeroconf, ips, port
        _LOG.info("Local name published: %s.local -> %s", self._label, ips[0])

    async def refresh(self) -> None:
        """Refresh the announcement if the network addresses changed."""
        if self._zeroconf is None:
            return
        ips = tuple(str(i.ip) for i in lan_interfaces(max_age=0))
        if ips != self._ips:
            port = self._port
            await self.stop()
            await self.start(port)

    async def stop(self) -> None:
        zeroconf, self._zeroconf = self._zeroconf, None
        self._ips = ()
        if zeroconf is not None:
            try:
                await zeroconf.async_unregister_all_services()
                await zeroconf.async_close()
            except Exception:
                pass


def install_lan_binding(loop: asyncio.AbstractEventLoop) -> None:
    """Bind the loop's outgoing connections to the adapter on the target's subnet."""
    orig_connection = loop.create_connection
    orig_datagram = loop.create_datagram_endpoint

    async def create_connection(protocol_factory, host=None, port=None, **kwargs):
        if (
            isinstance(host, str)
            and kwargs.get("local_addr") is None
            and kwargs.get("sock") is None
        ):
            for source in sources_for(host):
                try:
                    result = await orig_connection(
                        protocol_factory, host, port, local_addr=(source, 0), **kwargs
                    )
                except OSError as exc:
                    _LOG.debug("Connection via %s to %s failed: %s", source, host, exc)
                    continue
                _working_source[host] = source
                return result
        return await orig_connection(protocol_factory, host, port, **kwargs)

    async def create_datagram_endpoint(
        protocol_factory, local_addr=None, remote_addr=None, **kwargs
    ):
        if local_addr is None and remote_addr and kwargs.get("sock") is None:
            sources = sources_for(str(remote_addr[0]))
            if sources:
                local_addr = (sources[0], 0)
        return await orig_datagram(
            protocol_factory, local_addr=local_addr, remote_addr=remote_addr, **kwargs
        )

    loop.create_connection = create_connection  # type: ignore[method-assign]
    loop.create_datagram_endpoint = create_datagram_endpoint  # type: ignore[method-assign]
