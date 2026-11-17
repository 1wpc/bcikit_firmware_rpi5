"""HTTP bootstrap, reverse TCP and direct TCP with automatic protocol claim."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
from dataclasses import dataclass
from dataclasses import replace

from .device import Device
from .protocol import Parser, Request, cyton_frame, sample_frame

log = logging.getLogger(__name__)


@dataclass
class Session:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    owner_ip: str
    output: str = "auto"
    protocol: str = "auto"
    send_task: asyncio.Task | None = None
    receive_task: asyncio.Task | None = None


class Server:
    def __init__(self, device: Device, host: str = "0.0.0.0",
                 http_port: int = 8080, direct_port: int = 6677) -> None:
        self.device, self.host, self.http_port, self.direct_port = device, host, http_port, direct_port
        self.session: Session | None = None
        self.guard = asyncio.Lock()
        self.http_server = None
        self.direct_server = None

    async def run(self) -> None:
        self.http_server = await asyncio.start_server(self._http, self.host, self.http_port)
        self.direct_server = await asyncio.start_server(self._direct, self.host, self.direct_port)
        log.info("HTTP %s:%d; direct TCP %s:%d", self.host, self.http_port, self.host, self.direct_port)
        try:
            await asyncio.gather(self.http_server.serve_forever(), self.direct_server.serve_forever())
        finally:
            await self._close_session()
            self.device.backend.close()

    async def _close_session(self) -> None:
        async with self.guard:
            session = self.session
            self.session = None
            self.device.stop()
            if session:
                for task in (session.send_task, session.receive_task):
                    if task and task is not asyncio.current_task():
                        task.cancel()
                session.writer.close()
                with contextlib.suppress(Exception):
                    await session.writer.wait_closed()

    async def _attach(self, reader, writer, ip: str, output: str) -> Session:
        session = Session(reader, writer, ip, output, output if output != "auto" else "auto")
        self.session = session
        session.receive_task = asyncio.create_task(self._receive(session))
        session.send_task = asyncio.create_task(self._send(session))
        return session

    async def _direct(self, reader, writer) -> None:
        ip = writer.get_extra_info("peername")[0]
        async with self.guard:
            if self.session is not None:
                writer.close()
                return
            await self._attach(reader, writer, ip, "auto")

    async def _send(self, session: Session) -> None:
        try:
            while self.session is session:
                if not self.device.running.is_set() or session.protocol == "auto":
                    await asyncio.sleep(0.005)
                    continue
                try:
                    seq, ts, raw = await asyncio.to_thread(self.device.samples.get, True, 0.1)
                except Exception:
                    continue
                s = self.device.settings
                if session.protocol == "bcikit":
                    flags = 1 | (4 if self.device.backend.verified else 0)
                    if s.profile == 2:
                        flags |= 8
                    if s.profile == 3:
                        flags |= 16
                    packet = sample_frame(seq, ts, flags, s.sample_rate, s.board_count, s.gain_code, raw)
                else:
                    packet = cyton_frame(seq, raw)
                session.writer.write(packet)
                await session.writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            if self.session is session:
                await self._close_session()

    async def _receive(self, session: Session) -> None:
        parser = Parser()
        try:
            while self.session is session:
                data = await session.reader.read(4096)
                if not data:
                    break
                requests = None
                if session.protocol == "auto":
                    requests = parser.feed(data)
                    if requests:
                        session.protocol = "bcikit"
                        self.device.output_format = 0
                    elif data[:1] in (b"b", b"s", b"v", b"V", b"?", b"c", b"~"):
                        session.protocol = "raw"
                        self.device.output_format = 1
                    else:
                        continue
                if session.protocol == "bcikit":
                    for request in (requests if requests is not None else parser.feed(data)):
                        response = self.device.command(request, "bcikit")
                        session.writer.write(response)
                    await session.writer.drain()
                else:
                    for byte in data:
                        await self._openbci_command(session, chr(byte))
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            if self.session is session:
                await self._close_session()

    async def _openbci_command(self, session: Session, command: str) -> str:
        if command == "s":
            self.device.stop()
            return "OK"
        if command == "b":
            if self.device.settings.board_count != 1 or self.device.settings.gain_code != 6:
                return "Failure: Cyton requires one board and 24x gain$$$"
            return "OK" if self.device.start() else "Failure: ADS1299 configuration failed$$$"
        if command == "v":
            self.device.stop()
            settings = replace(self.device.settings, board_count=1, sample_rate=250,
                               profile=0, channel_mask=255, gain_code=6)
            if not self._openbci_configure(settings):
                return "Failure: ADS1299 configuration failed$$$"
            return "OpenBCI V3 8-16 channel\nFirmware: BCIKit Pi5 v0.1.0\n$$$"
        if command == "V":
            return "v0.1.0$$$"
        if command == "?":
            s = self.device.settings
            return f"ADS1299: ID=0x{self.device.backend.ads_id:02X} RATE={s.sample_rate} PROFILE={s.profile} CHANNEL_MASK=0x{s.channel_mask:02X}\n$$$"
        if command == "~~":
            return f"Sample rate is {self.device.settings.sample_rate}Hz$$$"
        if len(command) == 2 and command[0] == "~":
            rate = {"6": 250, "5": 500, "4": 1000}.get(command[1])
            if rate is None:
                return "Failure: unsupported OpenBCI WiFi sample rate$$$"
            settings = replace(self.device.settings, board_count=1, sample_rate=rate)
            return (f"Sample rate set to {rate}Hz$$$" if self._openbci_configure(settings)
                    else "Failure: ADS1299 configuration failed$$$")
        s = self.device.settings
        if command == "d":
            s = replace(s, profile=0, channel_mask=255)
        elif command == "0":
            s = replace(s, profile=3)
        elif command in "-=[]":
            s = replace(s, profile=2)
        elif command in "12345678":
            s = replace(s, channel_mask=s.channel_mask & ~(1 << (int(command) - 1)))
        elif command in "!@#$%^&*":
            s = replace(s, channel_mask=s.channel_mask | (1 << "!@#$%^&*".index(command)))
        else:
            return "Failure: unsupported OpenBCI command$$$"
        return ("Success: OpenBCI setting applied$$$" if self._openbci_configure(s)
                else "Failure: ADS1299 configuration failed$$$")

    def _openbci_configure(self, settings) -> bool:
        resume = self.device.running.is_set()
        if resume:
            self.device.stop()
        try:
            self.device.backend.configure(settings)
            self.device.settings = settings
            self.device.output_format = 1
            return self.device.start() if resume else True
        except Exception as exc:
            self.device.last_error = str(exc)
            self.device.counters[7] += 1
            return False

    async def _http(self, reader, writer) -> None:
        ip = writer.get_extra_info("peername")[0]
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 4)
            if len(header) > 8192:
                raise ValueError("header too large")
            lines = header.decode("latin1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            size = int(next((v.strip() for k, v in headers.items() if k.lower() == "content-length"), "0"))
            if size > 4096:
                raise ValueError("body too large")
            body = await asyncio.wait_for(reader.readexactly(size), 4) if size else b""
            status, payload, content_type = await self._route(method, path, ip, body)
            if isinstance(payload, dict):
                data = json.dumps(payload).encode()
                content_type = "application/json"
            else:
                data = str(payload).encode()
            phrase = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                      409: "Conflict", 500: "Internal Server Error", 504: "Gateway Timeout"}.get(status, "Error")
            writer.write(f"HTTP/1.1 {status} {phrase}\r\nContent-Type: {content_type}\r\nContent-Length: {len(data)}\r\nConnection: close\r\nAccess-Control-Allow-Origin: *\r\n\r\n".encode() + data)
            await writer.drain()
        except (ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
            log.warning("bad HTTP request from %s: %s", ip, exc)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _route(self, method: str, path: str, ip: str, body: bytes):
        text = "text/plain"
        if method == "GET" and path == "/board":
            return 200, {"board_connected": self.device.backend.verified,
                         "board_type": "cyton", "num_channels": 8,
                         "gains": [24] * 8}, text
        if method == "GET" and path == "/all":
            return 200, {"board_connected": self.device.backend.verified,
                         "ip": self.host, "name": "BCIKit-Pi5", "num_channels": 8,
                         "version": "0.1.0", "session": self.session.protocol if self.session else "none",
                         "latency": 10000, "last_error": self.device.last_error}, text
        if method == "GET" and path == "/version":
            return 200, "0.1.0", text
        if method == "GET" and path == "/tcp":
            return 200, {"connected": self.session is not None}, text
        if method == "POST" and path == "/tcp":
            try:
                request = json.loads(body)
                target_ip, port = request["ip"], int(request["port"])
                output = request.get("output", "raw")
                if target_ip != ip or port not in range(1, 65536) or output not in ("auto", "bcikit", "raw"):
                    return 403, "invalid target or output", text
            except (ValueError, KeyError, TypeError):
                return 400, "invalid JSON", text
            # Same host may replace a stale listener (e.g. Wi-Fi switch/crash).
            if self.session and self.session.owner_ip != ip:
                return 409, "BUSY: another host owns the session", text
            if self.session:
                await self._close_session()
            try:
                tcp_reader, tcp_writer = await asyncio.wait_for(asyncio.open_connection(target_ip, port), 3)
            except (OSError, asyncio.TimeoutError):
                return 504, "cannot connect to TCP target", text
            async with self.guard:
                await self._attach(tcp_reader, tcp_writer, ip, output)
                self.device.output_format = 1 if output == "raw" else 0
            return 200, {"connected": True, "ip": target_ip, "port": port, "output": output}, text
        if method == "DELETE" and path == "/tcp":
            if self.session and self.session.owner_ip != ip:
                return 409, "BUSY: another host owns the session", text
            await self._close_session()
            return 200, {"connected": False}, text
        if method == "GET" and path == "/stream/stop":
            self.device.stop()
            return 200, "OK", text
        if method == "GET" and path == "/stream/start":
            if not self.session or self.session.owner_ip != ip or self.session.protocol == "bcikit":
                return 409, "BUSY or no OpenBCI TCP session", text
            self.session.protocol = "raw"
            self.device.output_format = 1
            response = await self._openbci_command(self.session, "b")
            return (200 if response == "OK" else 500), response, text
        if method == "POST" and path == "/command":
            if not self.session or self.session.owner_ip != ip or self.session.protocol == "bcikit":
                return 409, "BUSY or no OpenBCI TCP session", text
            try:
                command = json.loads(body)["command"]
            except (ValueError, KeyError, TypeError):
                return 400, "invalid command", text
            if not isinstance(command, str) or not (1 <= len(command) <= 31):
                return 400, "unsupported command length", text
            self.session.protocol = "raw"
            response = await self._openbci_command(self.session, command)
            return (200 if not response.startswith("Failure:") else 400), response, text
        return 404, "Route Not Found", text
