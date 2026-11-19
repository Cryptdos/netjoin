"""Common types for the adapter back-ends."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

VIRTUAL_HINTS = (
    "hyper-v", "virtual", "vpn", "tap-", "wan miniport", "bluetooth", "loopback", "vethernet",
    "vmware", "virtualbox", "wireguard", "wintun", "tailscale", "zerotier", "teredo", "isatap",
    "npcap", "fortinet", "cisco anyconnect", "globalprotect", "pangp",
)


def looks_virtual(name: str, description: str) -> bool:
    text = f"{name} {description}".lower()
    return any(h in text for h in VIRTUAL_HINTS)


@dataclass
class Address:
    ip: str
    prefix: int
    dad: str | None = None      # preferred | tentative | duplicate | deprecated | invalid
    source: str | None = None   # manual | dhcp | other


@dataclass
class Adapter:
    name: str
    description: str = ""
    mac: str = ""
    up: bool = False
    virtual: bool = False
    dhcp: bool | None = None
    addresses: list[Address] = field(default_factory=list)
    gateways: list[str] = field(default_factory=list)
    dns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def primary(self) -> Address | None:
        good = [a for a in self.addresses if not a.ip.startswith("169.254.")]
        return (good or self.addresses or [None])[0]


class NetError(RuntimeError):
    """A command to change adapter settings failed."""


class Backend:
    kind = "base"

    def list_adapters(self) -> list[Adapter]:
        raise NotImplementedError

    def get_adapter(self, name: str) -> Adapter | None:
        for a in self.list_adapters():
            if a.name == name:
                return a
        return None

    def set_static(self, name: str, ip: str, prefix: int, gateway: str | None, dns: list[str]) -> None:
        raise NotImplementedError

    def set_dhcp(self, name: str) -> None:
        raise NotImplementedError

    def dad_state(self, name: str, ip: str) -> str | None:
        raise NotImplementedError

    def is_admin(self) -> bool:
        return True

    def wait_dad(self, name: str, ip: str, timeout: float = 4.0) -> str | None:
        return self.dad_state(name, ip)

    def check_internet(self, src_ip: str) -> bool:
        from ..inet import check_internet
        return check_internet(src_ip)
