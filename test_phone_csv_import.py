import ast
import csv
import unittest
from io import StringIO
from pathlib import Path

from core.phone_csv_import import (
    DID_CSV_HEADERS,
    PHONE_CSV_HEADERS,
    SPEED_DIAL_CSV_HEADERS,
    CSVImportLookups,
    parse_did_import_csv,
    parse_phone_import_csv,
    parse_speed_dial_import_csv,
)


ROOT = Path(__file__).resolve().parent


class PhoneCSVImportParserTests(unittest.TestCase):
    def test_phone_parser_plans_idempotent_update_and_normalizes_mac(self):
        lookups = CSVImportLookups(
            locations=frozenset({"hq"}),
            phones={"001122334455": {"model": "CP-9951", "is_active": False}},
            extensions_by_location={"hq": frozenset({"3000", "3001"})},
        )
        content = _csv_text(
            PHONE_CSV_HEADERS,
            [
                {
                    "location_slug": "hq",
                    "mac_address": "SEP00-11-22-33-44-55",
                    "model": "",
                    "label": "Reception",
                    "is_active": "",
                    "line_appearances": "1:3000:Primary",
                    "speed_dials": "1:Support:3001",
                }
            ],
        )

        result = parse_phone_import_csv(content, lookups=lookups, dry_run=True)

        self.assertTrue(result.valid)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.change_messages(), ["Row 2: update phone 001122334455"])
        row = result.rows[0]
        self.assertEqual(row.identifier, "001122334455")
        self.assertEqual(row.operation, "update")
        self.assertEqual(row.values["model"], "CP-9951")
        self.assertFalse(row.values["is_active"])
        self.assertEqual(row.line_appearances[0].extension_number, "3000")
        self.assertEqual(row.speed_dials[0].destination, "3001")

    def test_phone_parser_reports_bad_mac_and_invalid_extension_reference(self):
        lookups = CSVImportLookups(
            locations=frozenset({"hq"}),
            extensions_by_location={"hq": frozenset({"3000"})},
        )
        content = _csv_text(
            PHONE_CSV_HEADERS,
            [
                {
                    "location_slug": "hq",
                    "mac_address": "not-a-mac",
                    "model": "CP-9971",
                    "label": "Bad",
                    "is_active": "true",
                    "line_appearances": "1:3999:Missing",
                    "speed_dials": "",
                }
            ],
        )

        result = parse_phone_import_csv(content, lookups=lookups)

        self.assertFalse(result.valid)
        self.assertEqual(len(result.errors), 1)
        error = str(result.errors[0])
        self.assertIn("mac_address must be 12 hexadecimal characters", error)
        self.assertIn("line extension 3999 was not found in hq", error)


class DIDCSVImportParserTests(unittest.TestCase):
    def test_did_parser_plans_idempotent_update_by_number(self):
        lookups = CSVImportLookups(
            locations=frozenset({"hq"}),
            extensions_by_location={"hq": frozenset({"3000"})},
            dids={"+15551203000": {"location_slug": "hq", "is_active": False}},
            providers=frozenset({"carrier"}),
            trunks_by_location={"hq": frozenset({"primary"})},
            inbound_destinations_by_location={"hq": frozenset({"Main Menu"})},
        )
        content = _csv_text(
            DID_CSV_HEADERS,
            [
                {
                    "location_slug": "hq",
                    "number": "+15551203000",
                    "provider_slug": "carrier",
                    "trunk_name": "primary",
                    "direct_extension": "3000",
                    "default_destination": "Main Menu",
                    "label": "Main",
                    "is_active": "",
                }
            ],
        )

        result = parse_did_import_csv(content, lookups=lookups, dry_run=True)

        self.assertTrue(result.valid)
        self.assertEqual(result.change_messages(), ["Row 2: update did +15551203000"])
        row = result.rows[0]
        self.assertEqual(row.operation, "update")
        self.assertFalse(row.values["is_active"])
        self.assertEqual(row.values["direct_extension"], "3000")

    def test_did_parser_rejects_duplicate_dids_and_invalid_extension_reference(self):
        lookups = CSVImportLookups(
            locations=frozenset({"hq"}),
            extensions_by_location={"hq": frozenset({"3000"})},
        )
        content = _csv_text(
            DID_CSV_HEADERS,
            [
                {
                    "location_slug": "hq",
                    "number": "+15551203000",
                    "provider_slug": "",
                    "trunk_name": "",
                    "direct_extension": "3999",
                    "default_destination": "",
                    "label": "Main",
                    "is_active": "true",
                },
                {
                    "location_slug": "hq",
                    "number": "+15551203000",
                    "provider_slug": "",
                    "trunk_name": "",
                    "direct_extension": "3000",
                    "default_destination": "",
                    "label": "Duplicate",
                    "is_active": "true",
                },
            ],
        )

        result = parse_did_import_csv(content, lookups=lookups)

        self.assertFalse(result.valid)
        self.assertEqual(len(result.errors), 2)
        self.assertIn("direct_extension 3999 was not found in hq", str(result.errors[0]))
        self.assertIn("duplicate DID +15551203000 in CSV", str(result.errors[1]))


class SpeedDialCSVImportParserTests(unittest.TestCase):
    def test_speed_dial_parser_plans_update_by_phone_and_position(self):
        lookups = CSVImportLookups(
            phones={"001122334455": {"location_slug": "hq"}},
            speed_dials_by_phone={"001122334455": frozenset({1})},
        )
        content = _csv_text(
            SPEED_DIAL_CSV_HEADERS,
            [
                {
                    "phone_mac_address": "00:11:22:33:44:55",
                    "position": "1",
                    "label": "Support",
                    "destination": "3001",
                }
            ],
        )

        result = parse_speed_dial_import_csv(content, lookups=lookups, dry_run=True)

        self.assertTrue(result.valid)
        self.assertEqual(result.change_messages(), ["Row 2: update speed_dial 001122334455:1"])
        row = result.rows[0]
        self.assertEqual(row.operation, "update")
        self.assertEqual(row.values["phone_mac_address"], "001122334455")
        self.assertEqual(row.values["position"], 1)

    def test_speed_dial_parser_rejects_bad_rows(self):
        lookups = CSVImportLookups(phones={"001122334455": {"location_slug": "hq"}})
        content = _csv_text(
            SPEED_DIAL_CSV_HEADERS,
            [
                {
                    "phone_mac_address": "001122334455",
                    "position": "0",
                    "label": "",
                    "destination": "",
                },
                {
                    "phone_mac_address": "00-11-22-33-44-66",
                    "position": "1",
                    "label": "Support",
                    "destination": "3001",
                },
            ],
        )

        result = parse_speed_dial_import_csv(content, lookups=lookups)

        self.assertFalse(result.valid)
        self.assertIn("position must be a positive integer", str(result.errors[0]))
        self.assertIn("label is required", str(result.errors[0]))
        self.assertIn("destination is required", str(result.errors[0]))
        self.assertIn("phone_mac_address 001122334466 was not found", str(result.errors[1]))


class CSVImportDryRunAndAuditTests(unittest.TestCase):
    def test_dry_run_reports_intended_changes_without_mutating_fake_lookups(self):
        phones = {"001122334455": {"location_slug": "hq"}}
        lookups = CSVImportLookups(
            phones=phones,
            speed_dials_by_phone={"001122334455": frozenset()},
        )
        content = _csv_text(
            SPEED_DIAL_CSV_HEADERS,
            [
                {
                    "phone_mac_address": "001122334455",
                    "position": "2",
                    "label": "Sales",
                    "destination": "3002",
                }
            ],
        )

        result = parse_speed_dial_import_csv(content, lookups=lookups, dry_run=True)

        self.assertTrue(result.valid)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.change_messages(), ["Row 2: create speed_dial 001122334455:2"])
        self.assertEqual(phones, {"001122334455": {"location_slug": "hq"}})
        self.assertEqual(result.audit_events[0].outcome, "planned")

    def test_django_adapter_source_contains_dry_run_and_audit_gates(self):
        source = _source("core/phone_csv.py")
        for function_name in ("import_phones_csv", "import_dids_csv", "import_speed_dials_csv"):
            with self.subTest(function=function_name):
                body = _function_source(source, function_name)
                self.assertIn("if result.dry_run:", body)
                self.assertIn("return result", body)
                self.assertIn("record_config_change(", body)
                self.assertIn('source="csv_import"', body)

        rejection_body = _function_source(source, "_record_import_rejection")
        self.assertIn("record_audit(", rejection_body)
        self.assertIn("AuditOutcome.DENIED", rejection_body)
        self.assertIn('"errors": result.error_messages()', rejection_body)


def _csv_text(headers, rows):
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _source(relative_path):
    return (ROOT / relative_path).read_text()


def _function_source(source, function_name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f"Function not found: {function_name}")


if __name__ == "__main__":
    unittest.main()
