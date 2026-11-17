import struct
import unittest

from bcikit_pi5.ads1299 import Simulator
from bcikit_pi5.device import Device
from bcikit_pi5.protocol import (ACK, COMMAND, INFO, STATUS, Parser,
                                 control_frame, crc16, cyton_frame, sample_frame)


class ProtocolTest(unittest.TestCase):
    def test_known_crc(self):
        self.assertEqual(crc16(b"123456789"), 0x29B1)

    def test_incremental_parser_and_crc_reject(self):
        packet = control_frame(COMMAND, 42, 11)
        parser = Parser()
        self.assertEqual(parser.feed(packet[:4]), [])
        self.assertEqual(parser.feed(packet[4:])[0].request_id, 42)
        broken = bytearray(packet)
        broken[-1] ^= 1
        self.assertEqual(Parser().feed(broken), [])

    def test_info_status_layout(self):
        device = Device(Simulator(), 123, True)
        info, status = device.info(1), device.status(2)
        self.assertEqual(len(info), 38)
        self.assertEqual(len(status), 68)
        self.assertEqual(info[3], INFO)
        self.assertEqual(info[16], 0xFE)
        self.assertEqual(status[3], STATUS)

    def test_commands_and_sample(self):
        device = Device(Simulator(), 123, True)
        p = Parser()
        def command(code, payload=b""):
            request = p.feed(control_frame(COMMAND, code, code, payload=payload))[0]
            return device.command(request)
        self.assertEqual(command(12, b"\0")[9], 0)
        self.assertEqual(command(7, b"\2")[9], 0)
        self.assertEqual(command(3)[9], 0)
        seq, ts, raw = device.samples.get(timeout=1)
        frame = sample_frame(seq, ts, 5, 250, 2, 0, raw)
        self.assertEqual(len(frame), 82)
        self.assertEqual(struct.unpack_from("<H", frame, len(frame)-2)[0], crc16(frame[2:-2]))
        self.assertEqual(command(8, b"\1")[9], 4)  # BUSY while sampling
        self.assertEqual(command(4)[9], 0)
        self.assertFalse(device.running.is_set())

    def test_cyton_layout(self):
        raw = b"\xc0\0\0" + bytes(range(24))
        frame = cyton_frame(257, raw)
        self.assertEqual(len(frame), 33)
        self.assertEqual(frame[:2], b"\xa0\x01")
        self.assertEqual(frame[2:26], bytes(range(24)))
        self.assertEqual(frame[-1], 0xC0)


if __name__ == "__main__":
    unittest.main()
