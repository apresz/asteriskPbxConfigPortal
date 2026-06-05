from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
import zipfile

from .config_archive import json_bytes, manifest_entry, sha256sums, zip_archive
from .ivr_audio_export import (
    AudioPromptPayloadError,
    audio_prompt_archive_files,
    audio_prompt_payload_errors,
)
from .ivr_dialplan import (
    IVR_BUSINESS_HOURS_SCHEDULE_FIELDS,
    default_ivr_business_hours_schedule,
    incomplete_ivr_hours_destination_errors,
    render_ivr_dialplan_lines,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class IVRAudioPayloadTests(unittest.TestCase):
    def test_converted_prompt_file_is_selected_under_asterisk_sounds_payload_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            converted_path = Path(temp_dir) / "main-menu.wav"
            converted_path.write_bytes(b"RIFFasterisk-wav")
            prompt = {
                "ivr": "Main IVR",
                "prompt_name": "Main Menu",
                "converted_file": "audio_prompts/converted/hq/main-menu.wav",
                "converted_file_path": str(converted_path),
                "asterisk_path": "/var/lib/asterisk/sounds/custom/ivr/hq/main-menu.wav",
            }

            payload_files = audio_prompt_archive_files([prompt])
            manifest = {
                "files": [
                    manifest_entry(path, content, content_type)
                    for path, content, content_type in payload_files
                ],
            }
            archive_files = [
                *payload_files,
                ("manifest.json", json_bytes(manifest), "application/json"),
            ]
            archive_files.append(("SHA256SUMS", sha256sums(archive_files), "text/plain"))

            with zipfile.ZipFile(BytesIO(zip_archive(archive_files))) as archive:
                archived_names = set(archive.namelist())
                checksums = archive.read("SHA256SUMS").decode("utf-8")

        self.assertEqual(
            payload_files,
            [
                (
                    "asterisk/sounds/custom/ivr/hq/main-menu.wav",
                    b"RIFFasterisk-wav",
                    "audio/wav",
                )
            ],
        )
        self.assertIn("asterisk/sounds/custom/ivr/hq/main-menu.wav", archived_names)
        self.assertIn("  asterisk/sounds/custom/ivr/hq/main-menu.wav", checksums)
        self.assertEqual(
            manifest["files"][0]["path"],
            "asterisk/sounds/custom/ivr/hq/main-menu.wav",
        )

    def test_missing_converted_prompt_file_reports_export_error(self):
        prompt = {
            "ivr": "Main IVR",
            "prompt_name": "Main Menu",
            "converted_file": "audio_prompts/converted/hq/missing.wav",
            "converted_file_path": "/tmp/does-not-exist/main-menu.wav",
            "asterisk_path": "/var/lib/asterisk/sounds/custom/ivr/hq/main-menu.wav",
        }

        errors = audio_prompt_payload_errors([prompt])

        self.assertEqual(errors[0]["code"], "missing_ivr_prompt_file")
        self.assertEqual(errors[0]["ivr"], "Main IVR")
        with self.assertRaises(AudioPromptPayloadError):
            audio_prompt_archive_files([prompt])


class IVRHoursValidationTests(unittest.TestCase):
    def test_incomplete_hours_destinations_are_export_errors(self):
        errors = incomplete_ivr_hours_destination_errors(
            [
                {
                    "name": "Main IVR",
                    "business_hours_destination": {"type": "queue"},
                    "after_hours_destination": None,
                }
            ]
        )

        self.assertEqual(errors[0]["code"], "ivr_incomplete_hours_destinations")
        self.assertEqual(errors[0]["missing"], ["after_hours_destination"])

    def test_business_hours_schedule_exposes_gotoiftime_fields(self):
        schedule = default_ivr_business_hours_schedule("America/Los_Angeles")

        self.assertEqual(tuple(schedule), IVR_BUSINESS_HOURS_SCHEDULE_FIELDS)
        self.assertEqual(schedule["times"], "09:00-17:00")
        self.assertEqual(schedule["weekdays"], "mon-fri")
        self.assertEqual(schedule["monthdays"], "*")
        self.assertEqual(schedule["months"], "*")
        self.assertEqual(schedule["timezone"], "America/Los_Angeles")


class IVRDialplanGoldenTests(unittest.TestCase):
    maxDiff = None

    def test_business_and_after_hours_branches_render_golden_dialplan(self):
        ivr = {
            "name": "Main IVR",
            "prompt_name": "custom/ivr/hq/main-menu",
            "business_hours_destination": queue_destination("Support Queue"),
            "after_hours_destination": extension_destination("3000"),
            "business_hours_schedule": default_ivr_business_hours_schedule("America/Los_Angeles"),
            "timeout_seconds": 6,
            "timeout_destination": extension_destination("3000"),
            "invalid_destination": extension_destination("3000"),
            "menu_options": [],
        }

        self.assertEqual(
            render_text(ivr),
            (
                "exten => main-ivr,1,Goto(ivr-main-ivr,s,1)\n"
                "\n"
                "[ivr-main-ivr]\n"
                "exten => s,1,NoOp(IVR Main IVR)\n"
                " same => n,NoOp(IVR business_hours_schedule times=09:00-17:00 "
                "weekdays=mon-fri monthdays=* months=* timezone=America/Los_Angeles)\n"
                " same => n,GotoIfTime(09:00-17:00,mon-fri,*,*?business-hours,1)\n"
                " same => n,Goto(after-hours,1)\n"
                "\n"
                "exten => business-hours,1,NoOp(IVR Main IVR business hours)\n"
                " same => n,Goto(queues,support-queue,1)\n"
                " same => n,Hangup()\n"
                "\n"
                "exten => after-hours,1,NoOp(IVR Main IVR after hours)\n"
                " same => n,Goto(local-extensions,3000,1)\n"
                " same => n,Hangup()\n"
            ),
        )

    def test_timeout_invalid_input_and_digit_routes_render_golden_dialplan(self):
        ivr = {
            "name": "Menu IVR",
            "prompt_name": "custom/ivr/hq/menu",
            "business_hours_destination": None,
            "after_hours_destination": None,
            "business_hours_schedule": None,
            "timeout_seconds": 6,
            "timeout_destination": extension_destination("3000"),
            "invalid_destination": extension_destination("3001"),
            "menu_options": [
                {
                    "digit": "1",
                    "label": "Support",
                    "destination": queue_destination("Support Queue"),
                }
            ],
        }

        self.assertEqual(
            render_text(ivr),
            (
                "exten => menu-ivr,1,Goto(ivr-menu-ivr,s,1)\n"
                "\n"
                "[ivr-menu-ivr]\n"
                "exten => s,1,NoOp(IVR Menu IVR)\n"
                " same => n,Background(custom/ivr/hq/menu)\n"
                " same => n,WaitExten(6)\n"
                " same => n,Goto(t,1)\n"
                "\n"
                "exten => t,1,NoOp(IVR Menu IVR timeout)\n"
                " same => n,Goto(local-extensions,3000,1)\n"
                " same => n,Hangup()\n"
                "\n"
                "exten => i,1,NoOp(IVR Menu IVR invalid input)\n"
                " same => n,Goto(local-extensions,3001,1)\n"
                " same => n,Hangup()\n"
                "\n"
                "exten => 1,1,NoOp(IVR option 1 Support)\n"
                " same => n,Goto(queues,support-queue,1)\n"
                " same => n,Hangup()\n"
            ),
        )


class ConfigExportIVRSourceWiringTests(unittest.TestCase):
    def test_config_export_wires_prompt_files_schedule_and_dialplan_helpers(self):
        source = (PROJECT_ROOT / "core" / "config_export.py").read_text(encoding="utf-8")

        self.assertIn("audio_prompt_archive_files(_ivr_audio_prompt_refs(active_ivrs))", source)
        self.assertIn("audio_prompt_payload_errors(_ivr_audio_prompt_refs(active_ivrs))", source)
        self.assertIn("default_ivr_business_hours_schedule(timezone_name)", source)
        self.assertIn("render_ivr_dialplan_lines(", source)
        self.assertIn("./asterisk/sounds:/var/lib/asterisk/sounds:ro", source)

    def test_deployment_source_extracts_sounds_via_asterisk_bundle_root(self):
        source = (PROJECT_ROOT / "core" / "deployments.py").read_text(encoding="utf-8")

        self.assertIn('info.filename.startswith("asterisk/")', source)
        self.assertIn('"asterisk"', source)


def render_text(ivr):
    return "\n".join(
        render_ivr_dialplan_lines(
            ivr,
            slugify=slug,
            destination_app=destination_app,
        )
    )


def slug(value: str) -> str:
    return str(value).strip().lower().replace(" ", "-")


def destination_app(destination):
    if not destination:
        return "Playback(invalid)"
    if destination["type"] == "extension":
        return f"Goto(local-extensions,{destination['target']['number']},1)"
    if destination["type"] == "queue":
        return f"Goto(queues,{slug(destination['target']['name'])},1)"
    return "Playback(invalid)"


def extension_destination(number):
    return {
        "type": "extension",
        "target": {
            "number": number,
            "name": f"Extension {number}",
        },
    }


def queue_destination(name):
    return {
        "type": "queue",
        "target": {
            "id": 1,
            "name": name,
        },
    }


if __name__ == "__main__":
    unittest.main()
