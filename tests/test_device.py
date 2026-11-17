import unittest

from bcikit_pi5.ads1299 import FrameSyncError
from bcikit_pi5.device import Device
from bcikit_pi5.protocol import Request


class FakeBackend:
    def __init__(self):
        self.verified = False
        self.ads_id = 0
        self.configure_count = 0

    def configure(self, settings):
        self.configure_count += 1
        self.verified = True
        self.ads_id = 0x3E

    def stop(self):
        pass


class DeviceConfigurationTest(unittest.TestCase):
    def test_initial_unchanged_board_count_initializes_ads_before_start(self):
        backend = FakeBackend()
        device = Device(backend, device_id=1)
        ack = device.command(Request(1, 7, b"\x01"))

        self.assertEqual(ack[9], 0)
        self.assertEqual(backend.configure_count, 1)
        self.assertTrue(backend.verified)
        self.assertEqual(backend.ads_id, 0x3E)

        device.command(Request(2, 7, b"\x01"))
        self.assertEqual(backend.configure_count, 1)

    def test_single_bad_frame_does_not_abort_stream(self):
        class FlakyBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.device = None
                self.read_count = 0

            def read(self, _timeout):
                self.read_count += 1
                if self.read_count == 1:
                    raise FrameSyncError("bad sample")
                if self.read_count == 5:
                    self.device.running.clear()
                return 1, b"\xc0" + b"\0" * 26

        backend = FlakyBackend()
        device = Device(backend, device_id=1)
        backend.device = device
        device.running.set()
        device._sample_loop()

        self.assertEqual(device.counters[2], 1)
        self.assertEqual(device.counters[1], 1)
        self.assertEqual(device.samples.qsize(), 1)


if __name__ == "__main__":
    unittest.main()
