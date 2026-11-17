"""Run with python3 -m bcikit_pi5 --help."""

import argparse
import asyncio
import logging
import signal
import uuid

from .ads1299 import PiADS1299, Simulator
from .device import Device
from .server import Server


async def _run_server(server: Server) -> None:
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    await server.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="BCIKit AFE8 Raspberry Pi 5 host")
    parser.add_argument("--simulate", action="store_true", help="EXPLICIT synthetic data mode; never for patients")
    parser.add_argument("--external-clock", action="store_true", help="use independently verified external 2.048 MHz oscillator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--direct-port", type=int, default=6677)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    backend = Simulator() if args.simulate else PiADS1299(external_clock=args.external_clock)
    device = Device(backend, uuid.getnode(), simulate=args.simulate)
    try:
        asyncio.run(_run_server(Server(device, args.host, args.http_port, args.direct_port)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
