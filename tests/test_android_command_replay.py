"""Fake subprocess results exercise real ADB conversion without phone commands."""
import os
import subprocess
import unittest
from unittest.mock import patch

from phone_harness import android, cloud


class CloudCommandReplay(unittest.TestCase):
    def setUp(self):
        self.session = {"sid": "synthetic", "host": "phone.invalid", "port": 5555,
                        "code": "synthetic"}
        self.connected = True
        self.unlocked = True
        self.connect_ok = True
        self.command_error = None
        self.drop_during_command = False
        self.output = b"command output\n"
        self.commands = []
        self.adb_calls = []
        self.mutations = 0
        self.rented = self.session
        env = patch.dict(os.environ, {"ANDROID_SERIAL": "phone.invalid:5555",
                                     "PHONE_HARNESS_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        for target, kwargs in (
            ("phone_harness.android._adb_bin", {"return_value": "fake-adb"}),
            ("phone_harness.android.subprocess.run", {"side_effect": self.run_adb}),
            ("phone_harness.cloud.attached", {"side_effect": lambda: self.rented}),
            ("urllib.request.urlopen", {"side_effect": AssertionError("unexpected HTTP call")}),
        ):
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.phone = android.Android()
        self.phone._resolved = True
        self.phone._bounds = {"x": 0, "y": 0, "w": 720, "h": 1280,
                              "id": "phone.invalid:5555"}

    def run_adb(self, argv, **kwargs):
        self.adb_calls.append(argv)
        args = argv[1:]
        if args[:1] == ["-s"]:
            args = args[2:]
        status, out, err = 0, b"", b""
        if args == ["shell", "echo", "ph-ok"]:
            if not self.connected:
                status, err = 1, b"adb: device not found"
            else:
                out = b"ph-ok\n" if self.unlocked else b"locked\n"
        elif args[:1] == ["disconnect"]:
            self.connected = self.unlocked = False
        elif args[:1] == ["connect"]:
            self.connected = self.connect_ok
            out = b"connected\n" if self.connect_ok else b"failed to connect\n"
        elif args[:2] == ["shell", "unlock"]:
            self.unlocked = True
        elif args[:1] == ["shell"]:
            self.commands.append(args)
            if not self.connected:
                status, err = 1, b"adb: device not found"
            elif not self.unlocked:
                out = b"locked\n"
            else:
                self.mutations += 1
                if self.drop_during_command:
                    self.connected = False
                    status, err = 1, b"adb: error: connection closed"
                elif self.command_error is not None:
                    status, err = 1, self.command_error
                else:
                    out = self.output
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, status, out, err)

    def command(self, binary=False):
        return self.phone.send("raw", cmd="synthetic first-step && second-step", binary=binary)

    def test_application_nonzero_exit_is_not_replayed(self):
        self.command_error = b"second step failed after first step"
        with self.assertRaisesRegex(RuntimeError, "second step failed"):
            self.command()
        self.assertEqual(self.mutations, 1)
        self.assertEqual(len(self.commands), 1)

    def test_application_stderr_cannot_impersonate_a_transport_failure(self):
        self.command_error = b"adb: error: device offline"
        with self.assertRaises(RuntimeError):
            self.command()
        self.assertEqual(self.mutations, 1)
        self.assertEqual(len(self.commands), 1)

    def test_ambiguous_connection_loss_after_dispatch_is_not_replayed(self):
        self.drop_during_command = True
        with self.assertRaises(RuntimeError):
            self.command()
        self.assertEqual(self.mutations, 1)
        self.assertEqual(len(self.commands), 1)

    def test_disconnected_link_is_recovered_before_dispatch(self):
        self.connected = False
        self.assertEqual(self.command(), "command output\n")
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.mutations, 1)

    def test_locked_link_is_recovered_before_dispatch(self):
        self.unlocked = False
        self.assertEqual(self.command(), "command output\n")
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.mutations, 1)

    def test_literal_locked_output_is_returned_without_replay(self):
        for binary in (False, True):
            with self.subTest(binary=binary):
                self.commands.clear()
                self.mutations = 0
                self.output = b"locked\n"
                self.assertEqual(self.command(binary), self.output if binary else "locked\n")
                self.assertEqual(self.mutations, 1)
                self.assertEqual(len(self.commands), 1)

    def test_failed_preflight_reconnect_is_bounded_and_never_dispatches(self):
        self.connected = self.connect_ok = False
        with self.assertRaisesRegex(RuntimeError, "could not reach"):
            self.command()
        self.assertEqual(self.commands, [])
        self.assertEqual(len(self.adb_calls), 3)  # probe, disconnect, connect

    def test_binary_output_is_preserved(self):
        self.output = b"\x89PNG\x00\xff\r\n"
        self.assertEqual(self.command(binary=True), self.output)
        self.assertEqual(len(self.commands), 1)

    def test_physical_device_has_no_cloud_probe(self):
        self.rented = None
        self.assertEqual(self.command(), "command output\n")
        self.assertEqual(len(self.adb_calls), 1)


if __name__ == "__main__":
    unittest.main()
