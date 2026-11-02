"""
Split-brain healing: two PCs that both elected themselves leader (lost discovery
packet, firewall, simultaneous start) must merge, so only one advertises nearbeam.local.
"""

import unittest
from unittest import mock

from core.mesh import MeshManager


def make(name, started_at, role="leader"):
    roles = []
    m = MeshManager(port=5000, device_name=name, on_role_change=roles.append)
    m.started_at = started_at
    m.role = role
    m.electing = False
    m.roles = roles
    return m


class TestSplitBrainMerge(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(MeshManager, "_register_with_leader")
        self.register = patcher.start()
        self.addCleanup(patcher.stop)

    def test_newer_leader_steps_down_and_joins_older(self):
        old, new = make("OLD-PC", 100.0), make("NEW-PC", 200.0)
        new._handle_message(old._leader_announcement(), ("10.0.0.1", 5005))
        old._handle_message(new._leader_announcement(), ("10.0.0.2", 5005))
        self.assertEqual(new.role, "node")
        self.assertEqual(new.leader_info["node_id"], old.node_id)
        self.assertEqual(new.roles, ["node"])  # server stops advertising nearbeam.local on this
        self.assertEqual(old.role, "leader")
        self.assertEqual(old.roles, [])

    def test_same_start_time_breaks_tie_by_node_id(self):
        a, b = make("A", 100.0), make("B", 100.0)
        a.node_id, b.node_id = "aaa", "bbb"
        a._handle_message(b._leader_announcement(), None)
        b._handle_message(a._leader_announcement(), None)
        self.assertEqual((a.role, b.role), ("leader", "node"))

    def test_old_version_announcement_without_start_time_wins(self):
        new = make("NEW-PC", 200.0)
        legacy = {"type": "LEADER_ANNOUNCE", "node_id": "legacy_1", "name": "OLD", "ip": "10.0.0.9", "port": 5000}
        new._handle_message(legacy, None)
        self.assertEqual(new.role, "node")

    def test_announcement_during_startup_election_only_records_leader(self):
        m = make("BOOTING", 300.0)
        m.electing = True
        m._handle_message(make("OTHER", 100.0)._leader_announcement(), None)
        self.assertEqual(m.leader_info["name"], "OTHER")
        self.assertEqual(m.roles, [])  # _discover_and_elect decides; no double role change

    def test_node_follows_new_leader_and_reregisters(self):
        node = make("NODE", 50.0, role="node")
        first, second = make("L1", 100.0), make("L2", 90.0)
        node._handle_message(first._leader_announcement(), None)
        node._handle_message(first._leader_announcement(), None)  # same leader again: no re-register
        node._handle_message(second._leader_announcement(), None)
        self.assertEqual(node.leader_info["node_id"], second.node_id)
        self.assertEqual(self.register.call_count, 2)

    def test_node_sticks_with_better_leader_when_two_announce(self):
        """Two leaders still on the LAN (e.g. old versions): every node picks the same one, no flip-flop."""
        node_a, node_b = make("NODE-A", 500.0, role="node"), make("NODE-B", 600.0, role="node")
        older, newer = make("OLDER", 100.0), make("NEWER", 200.0)
        for node in (node_a, node_b):
            node._handle_message(newer._leader_announcement(), None)
            node._handle_message(older._leader_announcement(), None)
            node._handle_message(newer._leader_announcement(), None)  # must not switch back
        self.assertEqual(node_a.leader_info["node_id"], older.node_id)
        self.assertEqual(node_b.leader_info["node_id"], older.node_id)
        self.assertEqual(self.register.call_count, 4)  # each node: join NEWER, then move to OLDER once

    def test_own_messages_ignored(self):
        m = make("SELF", 100.0)
        m._handle_message(m._leader_announcement(), None)
        self.assertEqual(m.role, "leader")


if __name__ == "__main__":
    unittest.main()
