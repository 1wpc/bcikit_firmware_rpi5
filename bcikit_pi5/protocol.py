"""Wire-compatible BCIKit v1 control and sample frames."""

from __future__ import annotations

import struct
from dataclasses import dataclass

SYNC = b"\xa5\x5a"
VERSION = 1
SAMPLE, INFO, STATUS, ACK, EVENT, COMMAND = 1, 2, 3, 4, 5, 0x80
OK, INVALID_COMMAND, INVALID_LENGTH, INVALID_VALUE = 0, 1, 2, 3
BUSY, APPLY_FAILED, UNSUPPORTED = 4, 5, 6


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    return crc


def control_frame(kind: int, request_id: int, code: int = 0,
                  status: int = 0, payload: bytes = b"") -> bytes:
    body = struct.pack("<BBIBBH", VERSION, kind, request_id, code, status, len(payload)) + payload
    return SYNC + body + struct.pack("<H", crc16(body))


def sample_frame(sequence: int, timestamp_us: int, flags: int, rate: int,
                 boards: int, gain: int, raw: bytes) -> bytes:
    if boards not in range(1, 5) or len(raw) != boards * 27:
        raise ValueError("ADS1299 frame must contain 27 bytes per board")
    body = struct.pack("<BBHIQHBBBBH", VERSION, SAMPLE, flags, sequence,
                       timestamp_us, rate, boards, boards * 8, gain, 0, len(raw)) + raw
    return SYNC + body + struct.pack("<H", crc16(body))


def cyton_frame(sequence: int, raw: bytes) -> bytes:
    if len(raw) != 27:
        raise ValueError("OpenBCI Cyton supports one ADS1299 board")
    return b"\xa0" + bytes([sequence & 255]) + raw[3:] + b"\0" * 6 + b"\xc0"


@dataclass(frozen=True)
class Request:
    request_id: int
    code: int
    payload: bytes


class Parser:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[Request]:
        self.buffer.extend(data)
        out = []
        while True:
            at = self.buffer.find(SYNC)
            if at < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer.endswith(SYNC[:1]) else b""
                return out
            if at:
                del self.buffer[:at]
            if len(self.buffer) < 14:
                return out
            length = int.from_bytes(self.buffer[10:12], "little")
            if length > 32:
                del self.buffer[0]
                continue
            end = 14 + length
            if len(self.buffer) < end:
                return out
            body = bytes(self.buffer[2:end - 2])
            good = (self.buffer[2] == VERSION and self.buffer[3] == COMMAND
                    and self.buffer[9] == 0 and crc16(body) == int.from_bytes(self.buffer[end - 2:end], "little"))
            if good:
                out.append(Request(int.from_bytes(self.buffer[4:8], "little"),
                                   self.buffer[8], bytes(self.buffer[12:end - 2])))
                del self.buffer[:end]
            else:
                del self.buffer[0]
