from __future__ import annotations

import re
import unittest

from core.ring_group_dialplan import (
    RING_GROUP_STRATEGY_HUNT,
    RING_GROUP_STRATEGY_RING_ALL,
    RingGroupDialplanValidationError,
    render_ring_group_dialplan_lines,
)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def render(ring_group: dict) -> str:
    return "\n".join(render_ring_group_dialplan_lines(ring_group, slugify=slugify))


class RingGroupDialplanTests(unittest.TestCase):
    def test_ring_all_uses_simultaneous_dial_targets(self):
        dialplan = render(
            {
                "name": "Support Ring",
                "strategy": RING_GROUP_STRATEGY_RING_ALL,
                "timeout_seconds": 20,
                "members": [
                    {"extension": "3000", "priority": 1},
                    {"extension": "3001", "priority": 2},
                ],
            }
        )

        self.assertEqual(
            dialplan,
            "exten => support-ring,1,NoOp(Ring group Support Ring)\n"
            " same => n,Dial(PJSIP/3000&PJSIP/3001,20)\n"
            " same => n,Hangup()\n",
        )

    def test_hunt_dials_members_in_order_with_per_member_timeout(self):
        dialplan = render(
            {
                "name": "Support Hunt",
                "strategy": RING_GROUP_STRATEGY_HUNT,
                "timeout_seconds": 12,
                "members": [
                    {"extension": "3000", "priority": 1},
                    {"extension": "3001", "priority": 2},
                    {"extension": "3002", "priority": 3},
                ],
            }
        )

        self.assertEqual(
            dialplan,
            "exten => support-hunt,1,NoOp(Ring group Support Hunt)\n"
            " same => n,Dial(PJSIP/3000,12)\n"
            ' same => n,GotoIf($["${DIALSTATUS}" = "ANSWER"]?done)\n'
            " same => n,Dial(PJSIP/3001,12)\n"
            ' same => n,GotoIf($["${DIALSTATUS}" = "ANSWER"]?done)\n'
            " same => n,Dial(PJSIP/3002,12)\n"
            " same => n(done),Hangup()\n",
        )

    def test_hunt_preserves_final_fallback_after_last_attempt(self):
        dialplan = render(
            {
                "name": "Night Hunt",
                "strategy": RING_GROUP_STRATEGY_HUNT,
                "timeout_seconds": 7,
                "members": [
                    {"extension": "3100", "priority": 1},
                    {"extension": "3101", "priority": 2},
                ],
            }
        )

        self.assertIn(" same => n,Dial(PJSIP/3100,7)\n", dialplan)
        self.assertIn(" same => n,Dial(PJSIP/3101,7)\n", dialplan)
        self.assertTrue(dialplan.endswith(" same => n(done),Hangup()\n"))

    def test_empty_ring_group_is_rejected(self):
        with self.assertRaisesRegex(RingGroupDialplanValidationError, "at least one member"):
            render(
                {
                    "name": "Empty Ring",
                    "strategy": RING_GROUP_STRATEGY_RING_ALL,
                    "timeout_seconds": 20,
                    "members": [],
                }
            )

    def test_invalid_member_order_is_rejected(self):
        with self.assertRaisesRegex(RingGroupDialplanValidationError, "ordered by priority"):
            render(
                {
                    "name": "Bad Hunt",
                    "strategy": RING_GROUP_STRATEGY_HUNT,
                    "timeout_seconds": 20,
                    "members": [
                        {"extension": "3001", "priority": 2},
                        {"extension": "3000", "priority": 1},
                    ],
                }
            )

    def test_invalid_member_priority_is_rejected(self):
        with self.assertRaisesRegex(RingGroupDialplanValidationError, "positive integer"):
            render(
                {
                    "name": "Bad Priority",
                    "strategy": RING_GROUP_STRATEGY_HUNT,
                    "timeout_seconds": 20,
                    "members": [{"extension": "3000", "priority": 0}],
                }
            )


if __name__ == "__main__":
    unittest.main()
