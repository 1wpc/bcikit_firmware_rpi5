# BCIKit AFE8 Raspberry Pi 5 host

Python 3 program for BCIKit AFE8/ADS1299 on Raspberry Pi 5. It implements the **BCIKit Raw Stream v1** and **Device Control v1** command set, 1–4 daisy-chain boards, 250/500 SPS, uniform ADS1299 PGA gain codes 0–6, EEG common-reference / differential / internal-test / input-short profiles, channel mask, counters, and OpenBCI Cyton-format output for a single board at 24×. It includes the OpenBCI-style HTTP reverse-TCP bootstrap (`/board`, `/all`, `/tcp`, `/stream/*`, `/command`) plus a direct TCP endpoint. It does **not** currently implement BLE, UDP, SSDP/mDNS discovery, or a Wi-Fi hotspot; these limitations are not advertised in DEVICE_INFO. The OpenBCI-compatible layer has not yet been verified against OpenBCI GUI on real hardware.

This is an **engineering prototype, not a medical device**. Never connect electrodes to a person while the Pi, USB, mains-powered equipment, or unverified isolation is attached. One AFE board has now been bench-tested **with electrodes disconnected** on the target Pi: ADS ID and register verification passed, 100 consecutive raw frames were received at both 250 and 500 SPS, and the median DRDY gaps were 4000 and 2000 µs. The 500 SPS check used internal-test profile and 1× gain. Multi-board acquisition and OpenBCI GUI are still unverified on Pi hardware. Simulation remains opt-in (`--simulate`) and identifies itself with `hardware_type=0xFE`; normal mode never fabricates samples.

## Requirements and pin map

Target: Raspberry Pi 5, Raspberry Pi OS Bookworm 64-bit, Python 3.11, `python3-spidev` and `python3-libgpiod` (already installed on the target Pi). Enable SPI0 (`sudo raspi-config` → Interface Options → SPI) and reboot **after wiring is complete and power is off**. Do not change the Pi's active Wi-Fi settings merely to run this program. The default HTTP port is 8080 to avoid root privileges; pass `--http-port 80` if an OpenBCI host requires port 80 and arrange a safe port binding. The direct BCIKit TCP port is 6677.

| AFE signal (J3) | Pi BCM GPIO | 40-pin header | Notes |
|---|---:|---:|---|
| POWER_+5V | — | 2 or 4 | Verify power budget and polarity before wiring |
| GND | — | 6 | Common signal ground |
| CLK | 4 | 7 | **Continuous 2.048 MHz GPCLK0** required before AFE reset/SPI |
| SCLK | 11 | 23 | SPI0 SCLK |
| DIN | 10 | 19 | SPI0 MOSI |
| CHAIN_MISO_RETURN | 9 | 21 | SPI0 MISO |
| CS | 16 | 36 | Software CS; stays low across RDATAC reads |
| START | 27 | 13 | Output, safe low |
| RESET | 22 | 15 | Output, active low |
| PWDN | 23 | 16 | Output, active low |
| DRDY_CHAIN | 17 | 11 | Falling-edge input |

GPIO is **3.3 V logic only**. GPIO4's alternate-function setting is not enough: the clock generator must be active at 2.048 MHz. This program refuses real acquisition unless SPI0 is present and GPCLK0 is verified through `pinctrl` plus Linux clock debugfs, or `--external-clock` is explicitly used with an independently measured 2.048 MHz oscillator. The `clock/` directory contains a small Pi 5 RP1 clock-holder kernel module and systemd unit for the tested Raspberry Pi OS 6.6.51 kernel. It sets GPCLK0 to 2.048 MHz and uses `pinctrl` to route it to GPIO4 without changing Wi-Fi or the existing I²S sound-card overlay. A kernel update requires rebuilding/reinstalling the module. If possible, also verify CLK electrically with a scope/frequency counter; the software preflight is not a substitute for electrical measurement.

After checking the physical wiring and **with electrodes disconnected**, installation on the Pi is:

```bash
cd ~/Desktop/BCIKit-Pi5/clock
make
sudo install -D -m 0644 bcikit_gpclk.ko /lib/modules/$(uname -r)/extra/bcikit_gpclk.ko
sudo depmod -a
sudo install -m 0644 bcikit-gpclk.service /etc/systemd/system/bcikit-gpclk.service
sudo systemctl daemon-reload
sudo systemctl enable --now bcikit-gpclk.service
sudo raspi-config nonint do_spi 0
sudo reboot
```

Reconnect over SSH after reboot and check `systemctl is-active bcikit-gpclk`, `pinctrl get 4`, `ls -l /dev/spidev0.0`, and `grep clk_gp0 /sys/kernel/debug/clk/clk_summary`. The clock should have an enabled count greater than zero and a 2,048,000 Hz rate. To stop the clock, use `sudo systemctl disable --now bcikit-gpclk.service`; do not stop it during acquisition.

## Run

```bash
cd ~/Desktop/BCIKit-Pi5
python3 -m unittest discover -s tests -v
python3 -m bcikit_pi5 --simulate --host 127.0.0.1
```

Remove `--simulate` only after the AFE is correctly wired, SPI0 enabled and the 2.048 MHz clock verified. Real mode may expose HTTP diagnostics even when hardware is absent, but START and SET_* return `APPLY_FAILED` (status 5) instead of inventing data. Stop with Ctrl-C. `--help` lists all options. A manual, non-network, 60-frame hardware smoke test is available as `python3 -m bcikit_pi5.hardware_smoke --electrodes-disconnected`.

For a LAN host, use `http://<pi-ip>:8080`. The current BCIKit Studio `WiFiTransport` can be instantiated with `device_ip=<pi-ip>`, `device_port=8080`, and `require_softap_subnet=False`; its existing GUI currently assumes the ESP32 hotspot subnet and needs a Pi/LAN connection option. Alternatively, a custom client may connect directly to `<pi-ip>:6677` and send the same binary COMMAND frames. Do not switch the development Mac's Wi-Fi to a device hotspot.

### Raspberry Pi local workstation mode

For a Linux ARM64 Studio running **on the same Pi**, install the acquisition service after the clock/SPI setup above:

```bash
cd ~/Desktop/BCIKit-Pi5
sudo install -m 0644 systemd/bcikit-acquisition.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bcikit-acquisition.service
systemctl is-active bcikit-acquisition.service
```

This service binds HTTP and native TCP to `127.0.0.1` only. The Studio “树莓派本机” option connects directly to native TCP port 6677 and uses the usual BCIKit control/sample frames; it does not need a hotspot or the reverse-TCP HTTP setup. Only one Studio session can own the device at a time. `sudo systemctl stop bcikit-acquisition.service` stops acquisition and releases GPIO/SPI; the separate GPCLK service remains active. To use the Pi for a remote LAN client instead, stop this local service and run the program explicitly with `--host 0.0.0.0` on a trusted network.

The HTTP `/tcp` endpoint accepts a JSON object `{"ip":"<requesting-host-ip>","port":45123,"output":"auto"}` and opens a reverse TCP connection to that listener. The first valid BCIKit COMMAND claims native mode; `output:"raw"` selects OpenBCI. Only the same requester IP may replace a stale session; disconnect/DELETE automatically stops acquisition. For a native client, send `PING`, `GET_INFO`, `STOP`, `SET_*`, `START`, then read interleaved ACK and SAMPLE frames. See [PROTOCOL.md](PROTOCOL.md) for the complete public wire contract.

## Status and troubleshooting

GET_STATUS uses the same 54-byte payload as ESP32: acquisition state, output format, profile, board count, rate, gain, mask, register verification, ADS ID, next sequence, uptime, then eight `uint32` counters in order `drdy`, `frames`, `sync_error`, `spi_error`, `timeout`, `queue_overrun`, `transport_drop`, `register_verify_error`. `/all` adds a human-readable `last_error`. If `/dev/spidev0.0` or the 2.048 MHz clock disappears after a kernel update, rebuild/reinstall the clock module and verify the systemd service and SPI0 boot parameter before trying START.

## Open-source and submodule

MIT licensed. This repository can be included as a Git submodule in the private BCIKit project. Keep credentials, Wi-Fi secrets, and user data out of the public repository.
