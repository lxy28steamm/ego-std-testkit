import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from orangepi_inspection import main
from orangepi_profiles import resolve


class PatrolCliTests(unittest.TestCase):
    def invoke(self, args, state="已完成"):
        with patch.dict(os.environ, {"ORANGEPI_HOST": "test.local"}), \
                patch("orangepi_inspection.inspect", return_value={"state": state}) as inspect, \
                patch("orangepi_inspection.getpass.getpass", side_effect=AssertionError("interactive prompt")), \
                redirect_stdout(io.StringIO()):
            code = main(args)
        return code, inspect.call_args.args[0]

    def test_default_uses_environment_without_recording(self):
        code, config = self.invoke([])
        self.assertEqual((code, config["host"]), (0, "test.local"))
        self.assertFalse(config["test_record"])

    def test_record_uses_explicit_host_and_duration_without_prompting(self):
        code, config = self.invoke(["--host", "other.local", "--record", "--record-seconds", "10", "--device", "linked"])
        self.assertEqual((code, config["host"], config["record_seconds"]), (0, "other.local", 10))
        self.assertEqual(config["acceptance"], "linked")
        self.assertTrue(config["test_record"] and config["capture_preview"])
        self.assertFalse(config.get("test_upload"))

    def test_partial_failure_is_reported_to_shell(self):
        for state in ("检查失败", "检查未完成", "已停止"):
            with self.subTest(state=state):
                code, config = self.invoke(["--record"], state)
                self.assertEqual(code, 1)
                self.assertEqual(config["record_seconds"], 15)

    def test_invalid_arguments_never_contact_hardware(self):
        for args in (["--scope", "host", "--record"], ["--device", "system", "--record"],
                     ["--record-seconds", "15"], ["--record", "--record-seconds", "61"]):
            with self.subTest(args=args), patch("orangepi_inspection.inspect") as inspect, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["--host", "test.local", *args])
            self.assertEqual(error.exception.code, 2)
            inspect.assert_not_called()

    def test_auto_requires_exact_supported_configuration(self):
        def detect(slots):
            return resolve({"selection": {"enabled_devices": slots}})

        self.assertEqual(detect(["dex"])["slot"], "dex")
        self.assertEqual(set(detect(["dex", "ego-plus"])["linked_slots"]), {"dex", "ego-plus"})
        for slots in (["umi", "dex"], ["dex", "ego-plus", "unknown"]):
            with self.subTest(slots=slots), self.assertRaises(ValueError):
                detect(slots)
