"""One-owner acquisition state machine shared by every transport."""

from __future__ import annotations

import queue
import struct
import threading
import time
from dataclasses import replace

from .ads1299 import HardwareError, Settings
from .protocol import (ACK, APPLY_FAILED, BUSY, INFO, INVALID_COMMAND,
                       INVALID_LENGTH, INVALID_VALUE, OK, STATUS, UNSUPPORTED,
                       Request, control_frame)

# Bit 9 promises the full OpenBCI WiFi Shield HTTP/SSDP layer; the Pi host
# implements only the HTTP subset and must not advertise SSDP discovery.
CAPABILITIES = sum(1 << i for i in range(9)) | sum(1 << i for i in (10, 11, 12, 15))


class Device:
    def __init__(self, backend, device_id: int, simulate: bool = False) -> None:
        self.backend = backend
        self.device_id = device_id
        self.simulate = simulate
        self.settings = Settings()
        self.output_format = 0
        self.lock = threading.RLock()
        self.samples: queue.Queue[tuple[int, int, bytes]] = queue.Queue(maxsize=128)
        self.running = threading.Event()
        self.worker: threading.Thread | None = None
        self.sequence = 0
        self.counters = [0] * 8
        self.boot_us = time.monotonic_ns() // 1000
        self.last_error = ""

    def _sample_loop(self) -> None:
        discard = 3
        while self.running.is_set():
            try:
                sample = self.backend.read(0.5)
                if sample is None:
                    self.counters[4] += 1
                    continue
                self.counters[0] += 1
                timestamp, raw = sample
                if discard:
                    discard -= 1
                    continue
                seq = self.sequence
                self.sequence = (self.sequence + 1) & 0xFFFFFFFF
                self.counters[1] += 1
                try:
                    self.samples.put_nowait((seq, timestamp, raw))
                except queue.Full:
                    self.counters[5] += 1
            except HardwareError as exc:
                self.counters[2] += 1
                self.last_error = str(exc)
                self.running.clear()
            except Exception as exc:
                self.counters[3] += 1
                self.last_error = str(exc)
                self.running.clear()
        self.backend.stop()

    def start(self) -> bool:
        with self.lock:
            if self.running.is_set():
                return True
            try:
                self.backend.configure(self.settings)
                self.backend.start()
                self.samples = queue.Queue(maxsize=128)
                self.running.set()
                self.worker = threading.Thread(target=self._sample_loop, daemon=True)
                self.worker.start()
                self.last_error = ""
                return True
            except Exception as exc:
                self.counters[7] += 1
                self.last_error = str(exc)
                return False

    def stop(self) -> None:
        with self.lock:
            self.running.clear()
            worker = self.worker
            if worker and worker is not threading.current_thread():
                worker.join(timeout=1.0)
            self.backend.stop()
            self.worker = None

    def info(self, request_id: int) -> bytes:
        # hardware_type 2 = Pi 5, 0xFE = explicit simulator.
        payload = struct.pack("<BBBBBIBBBIQ", 0, 1, 0, 1,
                              0xFE if self.simulate else 2, CAPABILITIES,
                              4, 3, 3, 0, self.device_id)
        return control_frame(INFO, request_id, payload=payload)

    def status(self, request_id: int) -> bytes:
        s = self.settings
        payload = struct.pack("<BBBBHBBBBIQ8I", int(self.running.is_set()),
                              self.output_format, s.profile, s.board_count,
                              s.sample_rate, s.gain_code, s.channel_mask,
                              int(self.backend.verified), self.backend.ads_id,
                              self.sequence, time.monotonic_ns() // 1000 - self.boot_us,
                              *self.counters)
        return control_frame(STATUS, request_id, payload=payload)

    def command(self, request: Request, transport: str = "bcikit") -> bytes:
        code, value = request.code, request.payload
        expected = {1: 0, 2: 0, 3: 0, 4: 0, 5: 1, 6: 2, 7: 1,
                    8: 1, 9: 1, 10: 0, 12: 1}
        if code not in expected:
            if code != 11:
                return control_frame(ACK, request.request_id, code, INVALID_COMMAND)
        if code in expected and len(value) != expected[code]:
            return control_frame(ACK, request.request_id, code, INVALID_LENGTH)
        if code == 1:
            return self.info(request.request_id)
        if code == 2:
            return self.status(request.request_id)
        if code == 11:
            return control_frame(ACK, request.request_id, code, OK, value)
        if code == 4:
            self.stop()
            return control_frame(ACK, request.request_id, code)
        if code == 3:
            return control_frame(ACK, request.request_id, code,
                                 OK if self.start() else APPLY_FAILED)
        with self.lock:
            if self.running.is_set():
                return control_frame(ACK, request.request_id, code, BUSY)
            s = self.settings
            result = OK
            if code == 5:
                if value[0] > 1 or (value[0] == 1 and (s.board_count != 1 or s.gain_code != 6)):
                    result = INVALID_VALUE
                elif transport == "bcikit" and value[0] != 0:
                    result = INVALID_VALUE
                else:
                    self.output_format = value[0]
            elif code == 6:
                rate = int.from_bytes(value, "little")
                if rate not in (250, 500):
                    result = INVALID_VALUE
                else:
                    s = replace(s, sample_rate=rate)
            elif code == 7:
                if value[0] not in range(1, 5) or (self.output_format == 1 and value[0] != 1):
                    result = INVALID_VALUE
                else:
                    s = replace(s, board_count=value[0])
            elif code == 8:
                if value[0] not in range(4):
                    result = INVALID_VALUE
                else:
                    s = replace(s, profile=value[0])
            elif code == 9:
                s = replace(s, channel_mask=value[0])
            elif code == 12:
                if value[0] > 6:
                    result = INVALID_VALUE
                elif self.output_format == 1 and value[0] != 6:
                    result = UNSUPPORTED
                else:
                    s = replace(s, gain_code=value[0])
            elif code == 10:
                self.counters = [0] * 8
            if result == OK and s != self.settings:
                try:
                    self.backend.configure(s)
                    self.settings = s
                    self.last_error = ""
                except Exception as exc:
                    self.counters[7] += 1
                    self.last_error = str(exc)
                    result = APPLY_FAILED
            return control_frame(ACK, request.request_id, code, result)
