"""Short local-only ADS1299 data-path check; never opens a network socket."""

from __future__ import annotations

import argparse
import statistics

from .ads1299 import PiADS1299, Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Read raw ADS1299 frames on the Pi 5 GPIO/SPI bus")
    parser.add_argument("--electrodes-disconnected", action="store_true", required=False,
                        help="confirm no person or electrode cable is connected")
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--rate", type=int, choices=(250, 500), default=250)
    parser.add_argument("--profile", type=int, choices=range(4), default=0)
    parser.add_argument("--gain-code", type=int, choices=range(7), default=6)
    args = parser.parse_args()
    if not args.electrodes_disconnected:
        parser.error("unplug all electrodes and pass --electrodes-disconnected")
    if not 1 <= args.frames <= 500:
        parser.error("--frames must be in 1..500")
    backend = PiADS1299()
    samples = []
    try:
        settings = Settings(sample_rate=args.rate, profile=args.profile,
                            gain_code=args.gain_code)
        backend.configure(settings)
        print(f"ADS ID=0x{backend.ads_id:02X}, register_verified={backend.verified}")
        backend.start()
        for number in range(args.frames):
            sample = backend.read(0.5)
            if sample is None:
                print(f"DRDY timeout after {number} frames")
                break
            samples.append(sample)
    finally:
        backend.close()
    if samples:
        timestamps = [timestamp for timestamp, _ in samples]
        gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
        print(f"frames={len(samples)}, unique_payloads={len({raw for _, raw in samples})}, "
              f"status_first={samples[0][1][:3].hex()}, "
              f"status_last={samples[-1][1][:3].hex()}")
        if gaps:
            print(f"DRDY_gap_median_us={statistics.median(gaps):.1f}, "
                  f"min_us={min(gaps)}, max_us={max(gaps)}")
    if len(samples) != args.frames:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
