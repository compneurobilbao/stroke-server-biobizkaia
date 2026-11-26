"""Print the networks uvicorn may trust for ``X-Forwarded-For`` (``--forwarded-allow-ips``).

The API container runs behind the reverse proxy on the Compose network. It trusts proxy
headers only from that network: the IPv4 networks the container is directly attached to,
read from the kernel routing table (``/proc/net/route``), loopback excluded::

    python -m app.cli.forwarded_allow_ips      # e.g. 172.18.0.0/16

Without any such route (not Linux, no network) it prints ``127.0.0.1``. Uvicorn then takes
the client address from the right-most ``X-Forwarded-For`` entry that is not trusted, so a
client cannot spoof its address by sending the header itself. ``FORWARDED_ALLOW_IPS``
overrides the detected value (docker-compose.yml).
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import sys
from pathlib import Path

FALLBACK = "127.0.0.1"
ROUTE_TABLE = Path("/proc/net/route")


def _hex_to_ipv4(value: str) -> str:
    # /proc/net/route prints addresses as host-order (little-endian) hex.
    return socket.inet_ntoa(struct.pack("<L", int(value, 16)))


def attached_networks(route_table: str) -> list[str]:
    """IPv4 networks of the link routes (no gateway) in a ``/proc/net/route`` dump."""
    networks: list[str] = []
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8:
            continue
        iface, destination, gateway, mask = fields[0], fields[1], fields[2], fields[7]
        if iface == "lo" or int(gateway, 16) != 0 or int(destination, 16) == 0:
            continue
        try:
            network = ipaddress.ip_network(
                f"{_hex_to_ipv4(destination)}/{_hex_to_ipv4(mask)}", strict=False
            )
        except ValueError:
            continue
        if network.is_loopback or str(network) in networks:
            continue
        networks.append(str(network))
    return networks


def forwarded_allow_ips(route_file: Path = ROUTE_TABLE) -> str:
    try:
        table = route_file.read_text(encoding="ascii")
    except OSError:
        return FALLBACK
    return ",".join(attached_networks(table)) or FALLBACK


def main() -> int:
    sys.stdout.write(forwarded_allow_ips() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
