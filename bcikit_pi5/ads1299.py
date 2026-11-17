"""ADS1299 via SPI0 and RP1 GPIO; no silent fallback to synthetic samples."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    board_count: int = 1
    sample_rate: int = 250
    profile: int = 0
    channel_mask: int = 255
    gain_code: int = 6


class HardwareError(RuntimeError):
    pass


class PiADS1299:
    """Uses an externally configured 2.048 MHz GPCLK0 on GPIO4.

    The clock is deliberately not configured by this Python process: the
    RP1 clock driver/overlay must be installed and verified independently.
    """

    def __init__(self, *, external_clock: bool = False) -> None:
        self.external_clock = external_clock
        self.settings = Settings()
        self.verified = False
        self.ads_id = 0
        self.spi = None
        self.lines = {}
        self.chip = None
        self.streaming = False

    def preflight(self) -> None:
        if not os.path.exists("/dev/spidev0.0"):
            raise HardwareError("SPI0 disabled: enable dtparam=spi=on, reboot, then check /dev/spidev0.0")
        if not self.external_clock:
            try:
                from subprocess import check_output
                pin = check_output(["pinctrl", "get", "4"], text=True, timeout=2)
            except Exception as exc:
                raise HardwareError(f"cannot inspect GPIO4 GPCLK0: {exc}") from exc
            if "GPCLK0" not in pin:
                raise HardwareError("GPIO4 is not GPCLK0; 2.048 MHz ADS1299 clock is not configured")
            # An ALT function by itself does not prove a running, correct clock.
            try:
                with open("/sys/kernel/debug/clk/clk_summary", encoding="utf-8") as f:
                    rows = [r.split() for r in f if "clk_gp0" in r]
                if not any(len(r) >= 5 and int(r[1]) > 0 and abs(int(r[4]) - 2_048_000) < 1000 for r in rows):
                    raise ValueError("clock not enabled at 2.048 MHz")
            except Exception as exc:
                raise HardwareError(f"cannot verify active 2.048 MHz GPCLK0: {exc}") from exc

    def open(self) -> None:
        self.preflight()
        try:
            import gpiod
            import spidev
            self.chip = gpiod.Chip("gpiochip0")  # Verified RP1 header chip on the target Pi 5.
            mapping = {"cs": (16, 1), "start": (27, 0), "reset": (22, 1), "pwdn": (23, 1)}
            for name, (number, level) in mapping.items():
                line = self.chip.get_line(number)
                line.request(consumer="bcikit-pi5", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[level])
                self.lines[name] = line
            drdy = self.chip.get_line(17)
            drdy.request(consumer="bcikit-pi5", type=gpiod.LINE_REQ_EV_FALLING_EDGE)
            self.lines["drdy"] = drdy
            self.spi = spidev.SpiDev()
            self.spi.open(0, 0)
            self.spi.no_cs = True
            self.spi.mode = 1
            self.spi.max_speed_hz = 4_000_000
            self.spi.bits_per_word = 8
        except Exception as exc:
            self.close()
            raise HardwareError(f"GPIO/SPI open failed: {exc}") from exc

    def _set(self, name: str, level: int) -> None:
        self.lines[name].set_value(level)

    def _xfer(self, data: bytes) -> bytes:
        return bytes(self.spi.xfer2(list(data)))

    def _cmd(self, code: int) -> None:
        self._set("cs", 0)
        self._xfer(bytes([code]))
        self._set("cs", 1)
        time.sleep(0.00001)

    def _write(self, address: int, data: bytes) -> None:
        self._set("cs", 0)
        self._xfer(bytes([0x40 | address]))
        time.sleep(0.00001)
        self._xfer(bytes([len(data) - 1]))
        time.sleep(0.00001)
        self._xfer(data)
        self._set("cs", 1)
        time.sleep(0.00001)

    def _read(self, address: int) -> int:
        self._set("cs", 0)
        self._xfer(bytes([0x20 | address]))
        time.sleep(0.00001)
        self._xfer(b"\0")
        time.sleep(0.00001)
        value = self._xfer(b"\0")[-1]
        self._set("cs", 1)
        time.sleep(0.00001)
        return value

    def _power_cycle(self) -> None:
        self._set("start", 0)
        self._set("cs", 1)
        self._set("pwdn", 0)
        time.sleep(0.01)
        self._set("pwdn", 1)
        time.sleep(0.2)
        self._set("reset", 0)
        time.sleep(0.001)
        self._set("reset", 1)
        time.sleep(0.01)
        self._cmd(0x11)  # SDATAC

    def configure(self, settings: Settings) -> None:
        if self.spi is None:
            self.open()
        self.stop()
        self.verified = False
        config1 = {250: 0x96, 500: 0x95, 1000: 0x94}[settings.sample_rate]
        if settings.board_count > 1:
            config1 |= 0x40
        config2 = 0xD0 if settings.profile == 2 else 0xC0
        config3 = 0xE0 if settings.profile >= 2 else 0xEC
        channel = [0x68, 0x60, 0x65, 0x61][settings.profile]
        channels = bytes(((channel & 0x8F) | (settings.gain_code << 4))
                         if settings.channel_mask & (1 << i) else 0x81 for i in range(8))
        pos = 0 if settings.profile >= 2 else settings.channel_mask
        neg = settings.channel_mask if settings.profile == 1 else 0
        blocks = [(1, bytes([config1, config2, config3, 0])),
                  (5, channels + bytes([pos, neg, 0, 0, 0])),
                  (0x14, bytes([0x0F, 0, 0, 0]))]
        for attempt in range(3):
            self._power_cycle()
            for addr, data in blocks:
                self._write(addr, data)
            if settings.board_count > 1:
                self.settings, self.verified, self.ads_id = settings, True, 0
                return  # chained stream status validated after START
            self._write(1, bytes([config1 | 0x40]))
            checks = [(1, config1 | 0x40, 0x67), (2, config2, 0x17),
                      (3, config3, 0xFC), (4, 0, 0xFF)]
            checks += [(5 + i, channels[i], 0xFF) for i in range(8)]
            checks += [(0x0D, pos, 0xFF), (0x0E, neg, 0xFF)]
            checks += [(a, 0, 0xFF) for a in (0x0F, 0x10, 0x11)]
            checks += [(0x14, 0x0F, 0xFF), (0x15, 0, 0x20),
                       (0x16, 0, 0), (0x17, 0, 0x0F)]
            ok = all((self._read(a) & mask) == (expected & mask) for a, expected, mask in checks)
            self.ads_id = self._read(0)
            self._write(1, bytes([config1]))
            if ok and self.ads_id == 0x3E:
                self.settings, self.verified = settings, True
                return
        raise HardwareError(f"ADS1299 register verification failed (ID=0x{self.ads_id:02x})")

    def start(self) -> None:
        if not self.verified:
            self.configure(self.settings)
        self._set("start", 0)
        time.sleep(0.00001)
        self._set("start", 1)
        time.sleep(0.00005)
        self._set("cs", 0)
        self._xfer(b"\x10")  # RDATAC; keep CS low through every read
        time.sleep(0.00001)
        self.streaming = True

    def read(self, timeout: float = 0.5) -> tuple[int, bytes] | None:
        if not self.lines["drdy"].event_wait(timeout):
            return None
        event = self.lines["drdy"].event_read()
        timestamp_us = event.sec * 1_000_000 + event.nsec // 1000
        raw = self._xfer(bytes(self.settings.board_count * 27))
        if any((raw[i * 27] & 0xF0) != 0xC0 for i in range(self.settings.board_count)):
            raise HardwareError("ADS1299 stream status sync lost")
        return timestamp_us, raw

    def stop(self) -> None:
        if self.spi is None:
            return
        if self.streaming:
            self._set("cs", 1)
            time.sleep(0.00001)
            self._cmd(0x11)
        self._set("start", 0)
        self.streaming = False
        # A full chain power cycle is required before the next configuration.
        self.verified = False

    def close(self) -> None:
        self.stop()
        if self.spi is not None:
            self.spi.close()
            self.spi = None
        for line in self.lines.values():
            line.release()
        self.lines.clear()
        if self.chip is not None:
            self.chip.close()
            self.chip = None


class Simulator:
    """Explicit development-only backend; never selected implicitly."""

    def __init__(self) -> None:
        self.settings = Settings()
        self.verified = True
        self.ads_id = 0x3E
        self.streaming = False
        self.count = 0

    def configure(self, settings: Settings) -> None:
        self.settings = settings
        self.verified = True

    def start(self) -> None:
        self.streaming = True

    def read(self, timeout: float = 0.5) -> tuple[int, bytes]:
        time.sleep(1 / self.settings.sample_rate)
        self.count += 1
        block = b"\xc0\x00\x00" + b"\0" * 24
        return time.monotonic_ns() // 1000, block * self.settings.board_count

    def stop(self) -> None:
        self.streaming = False

    def close(self) -> None:
        self.stop()
