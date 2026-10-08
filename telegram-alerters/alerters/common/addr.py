"""Address helpers for EVM (0x, case-insensitive) and base58 chains (Tron, Solana - case-sensitive)."""
from __future__ import annotations

import hashlib

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(B58)}


def norm(address: str) -> str:
    """Canonical form: EVM hex is lower-cased, base58 is kept as is."""
    a = (address or "").strip()
    return a.lower() if a[:2] in ("0x", "0X") else a


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + _B58_IDX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\0" * pad + body


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def tron_from_hex(value: str) -> str:
    """'0x41ab..'/'41ab..'/'0xab..' (20 bytes) -> 'T...' base58check."""
    h = value.lower().removeprefix("0x")
    if len(h) == 40:
        h = "41" + h
    payload = bytes.fromhex(h[-42:])
    return b58encode(payload + _checksum(payload))


def tron_to_hex(address: str) -> str:
    """'T...' -> '0x' + 20-byte hex (EVM-style, without the 0x41 prefix)."""
    raw = b58decode(address)
    return "0x" + raw[1:21].hex()


def is_tron(address: str) -> bool:
    if not address or address[0] != "T" or len(address) != 34:
        return False
    try:
        raw = b58decode(address)
    except KeyError:
        return False
    return len(raw) == 25 and raw[0] == 0x41 and _checksum(raw[:21]) == raw[21:]


def is_solana(address: str) -> bool:
    if not address or not 32 <= len(address) <= 44:
        return False
    try:
        return len(b58decode(address)) == 32
    except KeyError:
        return False


TRON_ZERO = tron_from_hex("0x" + "00" * 20)  # T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb
