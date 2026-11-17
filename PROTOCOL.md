# BCIKit Pi 5 wire protocol

This implementation uses the same BCIKit Device Control v1 and Raw Stream v1 binary layouts as the ESP32 host. Integers in headers are little-endian; ADS1299 payload bytes remain MSB-first. CRC-16/CCITT-FALSE covers bytes from `version` through the payload, with polynomial `0x1021`, init `0xFFFF`, no reflection and no final XOR. Test vector: `123456789` → `0x29B1`.

## Control frame

| Offset | Bytes | Field |
|---:|---:|---|
| 0 | 2 | `A5 5A` sync |
| 2 | 1 | version `01` |
| 3 | 1 | type (`02` info, `03` status, `04` ACK, `80` command) |
| 4 | 4 | request ID |
| 8 | 1 | command code (or echoed code in ACK) |
| 9 | 1 | status; commands send zero |
| 10 | 2 | payload length (max 32 for commands) |
| 12 | N | payload |
| 12+N | 2 | CRC, little-endian |

Total `14+N` bytes. ACK has the matching request ID and code. GET_INFO and GET_STATUS respond with their own frame types, not ACK.

| Code | Command | Request payload | Response |
|---:|---|---|---|
| `01` | GET_INFO | empty | DEVICE_INFO |
| `02` | GET_STATUS | empty | DEVICE_STATUS |
| `03` | START | empty | ACK |
| `04` | STOP | empty | ACK |
| `05` | SET_OUTPUT_FORMAT | `u8`: 0 BCIKit, 1 OpenBCI | ACK; native TCP accepts 0 |
| `06` | SET_SAMPLE_RATE | `u16`: 250 or 500 | ACK |
| `07` | SET_BOARD_COUNT | `u8`: 1–4 | ACK |
| `08` | SET_PROFILE | `u8`: 0 common-reference SRB2, 1 differential, 2 internal test, 3 input short | ACK |
| `09` | SET_CHANNEL_MASK | `u8` bit 0–7 = CH1–CH8 | ACK |
| `0A` | RESET_STATS | empty | ACK |
| `0B` | PING | 0–32 arbitrary bytes | ACK echoes payload |
| `0C` | SET_GAIN | `u8` code 0–6 | ACK |

Gain codes 0–6 correspond to 1×, 2×, 4×, 6×, 8×, 12×, 24×. One code applies to all enabled channels on all boards. Stop before changing configuration. ACK status: 0 OK, 1 invalid command, 2 invalid length, 3 invalid value, 4 busy/streaming, 5 hardware apply/verification failure, 6 unsupported. Bad-CRC command frames are discarded.

DEVICE_INFO payload is 24 bytes: `u8 firmware_major,minor,patch,protocol_version,hardware_type; u32 capabilities; u8 max_board_count,sample_rate_mask,output_format_mask; u32 serial_baud; u64 device_id`. This host uses hardware type 2 for Pi 5, `0xFE` for explicit simulation; serial baud is zero because the interface is TCP. Capabilities bits 9 (complete OpenBCI HTTP/SSDP), 13 and 14 (BLE) are clear. Bits 0–8, 10–12 and 15 indicate the implemented binary stream/control, HTTP/TCP session and gain support. The Pi's OpenBCI HTTP subset does not imply SSDP discovery or GUI compatibility.

DEVICE_STATUS payload is 54 bytes: `u8 acquisition_state,output_format,profile,board_count; u16 sample_rate; u8 gain_code,channel_mask,register_verified,ads_id; u32 next_sequence; u64 uptime_us; u32 counters[8]`. Counter order: DRDY, frames, sync errors, SPI errors, DRDY timeouts, queue overruns, transport drops, register verification errors.

## Sample frame

| Offset | Bytes | Field |
|---:|---:|---|
| 0 | 2 | `A5 5A` sync |
| 2 | 1 | version `01` |
| 3 | 1 | type `01` |
| 4 | 2 | flags |
| 6 | 4 | sequence |
| 10 | 8 | DRDY monotonic timestamp, microseconds |
| 18 | 2 | rate, Hz |
| 20 | 1 | board count |
| 21 | 1 | channel count = 8 × board count |
| 22 | 1 | gain code |
| 23 | 1 | reserved zero |
| 24 | 2 | payload length = 27 × board count |
| 26 | N | raw ADS1299 board blocks |
| 26+N | 2 | CRC |

Total `28+27*board_count` bytes. Each board block is 3 status bytes followed by eight signed 24-bit channel values, all in original wire order. Flags bit 0 = valid timestamp, bit 2 = registers verified, bit 3 = internal-test profile, bit 4 = input-short profile. The host must split/reassemble TCP bytes regardless of `recv()` boundaries and distinguish control from sample by frame type. OpenBCI output, when selected, is the 33-byte Cyton packet (`A0`, sequence low byte, 24 channel bytes, six zero auxiliary bytes, `C0`); only one board and 24× gain are supported there.

## Transport

The HTTP service defaults to `:8080`, direct binary TCP to `:6677`. `GET /board` and `GET /all` give discovery/diagnostics. The existing Studio-style reverse TCP path is: host listens locally → `POST /tcp` with its own IP, port and `output:"auto"` → Pi connects back → host sends PING and continues all commands/samples on the same TCP socket. `DELETE /tcp` stops and releases the session. OpenBCI clients may choose `output:"raw"` and use `GET /stream/start`, `GET /stream/stop`, and `POST /command` with a JSON `command` string. The Pi does not create an access point; use an existing LAN and configure the Studio transport to allow a non-hotspot subnet.
