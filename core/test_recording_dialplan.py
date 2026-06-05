from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest

from core.asterisk_config_helpers import (
    recording_hook_context_lines,
    recording_policy_hook_lines,
    recording_policy_requires_mixmonitor,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RecordingPolicyHookGoldenTests(unittest.TestCase):
    def test_extension_policy_renders_mixmonitor_hook(self):
        lines = recording_policy_hook_lines("extension", "3000", "always", include_context=True)
        rendered = "\n".join(lines)

        self.assertIn(" same => n,Gosub(recording-hooks,s,1(extension,3000,always))", rendered)
        self.assertIn(" same => n,MixMonitor(/var/spool/asterisk/monitor/${RECORDING_FILE},b)", rendered)

    def test_queue_policy_renders_mixmonitor_hook(self):
        lines = recording_policy_hook_lines("queue", "Support Queue", "on_demand", include_context=True)
        rendered = "\n".join(lines)

        self.assertIn(" same => n,Gosub(recording-hooks,s,1(queue,support-queue,on_demand))", rendered)
        self.assertIn(" same => n,MixMonitor(/var/spool/asterisk/monitor/${RECORDING_FILE},b)", rendered)

    def test_route_policy_renders_mixmonitor_hook(self):
        lines = recording_policy_hook_lines("route", "National", "always", include_context=True)
        rendered = "\n".join(lines)

        self.assertIn(" same => n,Gosub(recording-hooks,s,1(route,national,always))", rendered)
        self.assertIn(" same => n,MixMonitor(/var/spool/asterisk/monitor/${RECORDING_FILE},b)", rendered)

    def test_policy_object_values_are_supported(self):
        policy = SimpleNamespace(value="always")

        self.assertTrue(recording_policy_requires_mixmonitor(policy))
        self.assertEqual(
            recording_policy_hook_lines("extension", "3000", policy),
            [" same => n,Gosub(recording-hooks,s,1(extension,3000,always))"],
        )

    def test_off_policy_renders_no_recording_commands(self):
        self.assertFalse(recording_policy_requires_mixmonitor("never"))
        for source_type, source_id, policy in (
            ("extension", "3000", "never"),
            ("queue", "Support Queue", ""),
            ("route", "National", None),
        ):
            with self.subTest(source_type=source_type):
                rendered = "\n".join(recording_policy_hook_lines(source_type, source_id, policy, include_context=True))

                self.assertEqual(rendered, "")
                self.assertNotIn("Gosub(recording-hooks", rendered)
                self.assertNotIn("MixMonitor", rendered)

    def test_recording_context_sets_deterministic_file_and_correlation_metadata(self):
        rendered = "\n".join(recording_hook_context_lines())

        self.assertIn("[recording-hooks]", rendered)
        self.assertIn(
            " same => n,Set(__RECORDING_FILE=${UNIQUEID}-${LOCAL_PBX}-${RECORDING_SOURCE}-${RECORDING_POLICY}.wav)",
            rendered,
        )
        self.assertIn(
            " same => n,Set(CDR(userfield)=recording_file=${RECORDING_FILE} "
            "recording_source=${RECORDING_SOURCE} recording_policy=${RECORDING_POLICY})",
            rendered,
        )
        self.assertIn(" same => n,MixMonitor(/var/spool/asterisk/monitor/${RECORDING_FILE},b)", rendered)


class ConfigExportRecordingHookWiringTests(unittest.TestCase):
    def test_config_export_source_wires_hooks_to_extension_queue_and_route_dialplan(self):
        source = (PROJECT_ROOT / "core" / "config_export.py").read_text(encoding="utf-8")

        self.assertIn('recording_policy_hook_lines("extension", extension.number, extension.recording_policy)', source)
        self.assertIn('recording_policy_hook_lines("queue", queue_name, queue.get("recording_policy"))', source)
        self.assertIn('recording_policy_hook_lines("route", _slug(route.name), route.recording_policy)', source)
        self.assertIn("recording_hook_context_lines()", source)


if __name__ == "__main__":
    unittest.main()
