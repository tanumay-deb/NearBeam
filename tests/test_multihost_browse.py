"""
Tests for Option 1 Multi-Host Shared Folders fix:
- Leader returns full cluster peer snapshot on /api/mesh/register & /api/mesh/heartbeat
- Secondary nodes sync sibling peers so 3+ PC clusters see every host from every PC
- Invalid peer IP/port registrations are rejected (URL/XSS/SSRF hardening)
- Browse Host Switcher & Nearby Devices Browse button markup/JS + strict CORS preserved
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import core.server as srv_mod
from core.config import ConfigManager
from core.mesh import MeshManager, MeshNode
from core.server import NearBeamServer


class _FakeHTTPResp:
    def __init__(self, payload: dict, status: int = 200):
        self.status = status
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestMultiHostPeerSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nb_multihost_")
        self.cfg = ConfigManager(os.path.join(self.tmp, "cfg.json"))
        self.cfg.set_save_directory(os.path.join(self.tmp, "recv"))
        self.orig_cfg = srv_mod.config
        srv_mod.config = self.cfg

    def tearDown(self):
        srv_mod.config = self.orig_cfg
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_server(self, name="Leader-A", ip="192.168.1.10", port=5000, role="leader"):
        srv = NearBeamServer(port=port, enable_mesh=False)
        mesh = MeshManager(port=port, device_name=name)
        mesh.ip = ip
        mesh.role = role
        mesh.electing = False
        srv.mesh = mesh
        return srv

    def test_leader_register_and_heartbeat_return_full_cluster_nodes(self):
        srv = self._make_server("PC-Alpha", "192.168.1.10", 5000, role="leader")
        client = srv.app.test_client()
        try:
            r1 = client.post(
                "/api/mesh/register",
                json={"node_id": "node-beta", "name": "PC-Beta", "ip": "192.168.1.20", "port": 5001},
            )
            self.assertEqual(r1.status_code, 200)
            d1 = r1.get_json()
            self.assertIn("nodes", d1)
            ids1 = {n["id"] for n in d1["nodes"]}
            self.assertEqual(ids1, {srv.mesh.node_id, "node-beta"})

            r2 = client.post(
                "/api/mesh/register",
                json={"node_id": "node-gamma", "name": "PC-Gamma", "ip": "192.168.1.30", "port": 5002},
            )
            self.assertEqual(r2.status_code, 200)
            d2 = r2.get_json()
            ids2 = {n["id"] for n in d2["nodes"]}
            self.assertEqual(ids2, {srv.mesh.node_id, "node-beta", "node-gamma"})
            # Snapshot names must be clean hostnames without "(This PC)" suffix
            leader_entry = next(n for n in d2["nodes"] if n["id"] == srv.mesh.node_id)
            self.assertEqual(leader_entry["name"], "PC-Alpha")
            self.assertEqual(leader_entry["role"], "leader")

            hb = client.post("/api/mesh/heartbeat", json={"node_id": "node-beta"})
            self.assertEqual(hb.status_code, 200)
            hb_data = hb.get_json()
            self.assertTrue(hb_data["updated"])
            self.assertIn("nodes", hb_data)
            self.assertEqual({n["id"] for n in hb_data["nodes"]}, {srv.mesh.node_id, "node-beta", "node-gamma"})
        finally:
            srv.shutdown()

    def test_register_rejects_invalid_ip_and_port(self):
        srv = self._make_server("PC-Alpha", "192.168.1.10", 5000, role="leader")
        client = srv.app.test_client()
        try:
            for bad_payload in (
                {"node_id": "n1", "name": "Bad", "ip": "javascript:alert(1)", "port": 5000},
                {"node_id": "n2", "name": "Bad", "ip": "192.168.1.20/evil", "port": 5000},
                {"node_id": "n3", "name": "Bad", "ip": "192.168.1.20", "port": 0},
                {"node_id": "n4", "name": "Bad", "ip": "192.168.1.20", "port": 70000},
                {"node_id": "n5", "name": "Bad", "ip": "192.168.1.20", "port": "abc"},
                {"node_id": "n6", "name": "Bad", "ip": "192.168.1.20", "port": True},
            ):
                r = client.post("/api/mesh/register", json=bad_payload)
                self.assertEqual(r.status_code, 400, (bad_payload, r.data))
            self.assertEqual(srv.mesh.nodes, {})
        finally:
            srv.shutdown()

    def test_secondary_node_syncs_sibling_peers_without_duplicates_and_notifies_changes(self):
        changes = []
        node = MeshManager(port=5001, device_name="PC-Beta", on_nodes_change=changes.append)
        node.ip = "192.168.1.20"
        node.role = "node"
        node.electing = False
        node.leader_info = {
            "node_id": "leader-alpha",
            "name": "PC-Alpha",
            "ip": "192.168.1.10",
            "port": 5000,
            "started_at": 100.0,
        }

        cluster_3 = [
            {"id": "leader-alpha", "name": "PC-Alpha", "ip": "192.168.1.10", "port": 5000, "role": "leader"},
            {"id": node.node_id, "name": "PC-Beta", "ip": "192.168.1.20", "port": 5001, "role": "node"},
            {"id": "node-gamma", "name": "PC-Gamma", "ip": "192.168.1.30", "port": 5002, "role": "node"},
            # Malformed peer entries must be ignored silently
            {"id": "evil-1", "name": "Evil", "ip": "javascript:alert(1)", "port": 5000, "role": "node"},
            {"id": "evil-2", "name": "BadPort", "ip": "192.168.1.40", "port": 99999, "role": "node"},
            "not-a-dict",
        ]

        with mock.patch("core.mesh.urllib.request.urlopen") as m_open:
            m_open.return_value = _FakeHTTPResp({
                "success": True,
                "leader_id": "leader-alpha",
                "leader_name": "PC-Alpha",
                "leader_role": "leader",
                "leader_started_at": 100.0,
                "nodes": cluster_3,
            })
            node._register_with_leader()

        nodes_list = node.get_nodes_list()
        ids = [n["id"] for n in nodes_list]
        self.assertEqual(ids, [node.node_id, "leader-alpha", "node-gamma"])
        self.assertEqual(len(changes), 1)
        gamma = next(n for n in nodes_list if n["id"] == "node-gamma")
        self.assertEqual(gamma["url"], "http://192.168.1.30:5002")
        self.assertEqual(gamma["raw_name"], "PC-Gamma")
        self.assertFalse(gamma["is_self"])
        self.assertFalse(gamma["is_leader"])

        # Same peer list again must NOT re-trigger on_nodes_change
        node._sync_cluster_peers(cluster_3)
        self.assertEqual(len(changes), 1)

        # Sibling departs -> pruned and callback fires
        node._sync_cluster_peers([
            {"id": "leader-alpha", "name": "PC-Alpha", "ip": "192.168.1.10", "port": 5000, "role": "leader"},
            {"id": node.node_id, "name": "PC-Beta", "ip": "192.168.1.20", "port": 5001, "role": "node"},
        ])
        self.assertEqual([n["id"] for n in node.get_nodes_list()], [node.node_id, "leader-alpha"])
        self.assertEqual(len(changes), 2)

        # Promoting to leader clears cached secondary peers
        node._sync_cluster_peers(cluster_3)
        self.assertEqual(len(node.get_nodes_list()), 3)
        with mock.patch.object(node, "_send_broadcast"):
            node.promote_to_leader()
        self.assertEqual([n["id"] for n in node.get_nodes_list()], [node.node_id])

    def test_secondary_node_nearby_devices_and_clipboard_relay(self):
        srv = self._make_server("PC-Beta", "192.168.1.20", 5001, role="node")
        srv.mesh.leader_info = {
            "node_id": "leader-alpha",
            "name": "PC-Alpha",
            "ip": "192.168.1.10",
            "port": 5000,
            "started_at": 100.0,
        }
        srv.mesh._sync_cluster_peers([
            {"id": "leader-alpha", "name": "PC-Alpha", "ip": "192.168.1.10", "port": 5000, "role": "leader"},
            {"id": srv.mesh.node_id, "name": "PC-Beta", "ip": "192.168.1.20", "port": 5001, "role": "node"},
            {"id": "node-gamma", "name": "PC-Gamma", "ip": "192.168.1.30", "port": 5002, "role": "node"},
        ])
        try:
            nearby = srv.get_nearby_devices()
            hosts = [d for d in nearby if d["kind"] == "host"]
            self.assertEqual({h["id"] for h in hosts}, {srv.mesh.node_id, "leader-alpha", "node-gamma"})
            gamma = next(h for h in hosts if h["id"] == "node-gamma")
            self.assertEqual(gamma["url"], "http://192.168.1.30:5002")

            # Clipboard from secondary still relays ONLY to leader (not directly to siblings)
            called_urls = []
            with mock.patch("core.mesh.urllib.request.urlopen") as m_open:
                m_open.side_effect = lambda req, timeout=4.0: (called_urls.append(req.full_url), _FakeHTTPResp({}))[1]
                srv.mesh.broadcast_clipboard_mesh("hello cluster")
                # Give daemon thread a brief moment
                import time
                time.sleep(0.15)
            self.assertEqual(called_urls, ["http://192.168.1.10:5000/api/mesh/clipboard-sync"])
        finally:
            srv.shutdown()

    def test_older_leader_without_nodes_field_keeps_leader_visible(self):
        node = MeshManager(port=5001, device_name="PC-Beta")
        node.ip = "192.168.1.20"
        node.role = "node"
        node.leader_info = {
            "node_id": "leader-old",
            "name": "PC-Old",
            "ip": "192.168.1.10",
            "port": 5000,
            "started_at": 100.0,
        }
        with mock.patch("core.mesh.urllib.request.urlopen") as m_open:
            m_open.return_value = _FakeHTTPResp({"success": True, "leader_role": "leader"})
            node._register_with_leader()
        ids = [n["id"] for n in node.get_nodes_list()]
        self.assertEqual(ids, [node.node_id, "leader-old"])

    def test_browse_host_switcher_ui_and_strict_cors_preserved(self):
        srv = self._make_server("PC-Alpha", "192.168.1.10", 5000, role="leader")
        client = srv.app.test_client()
        try:
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn('id="browseHostSwitcher"', html)
            self.assertIn('id="browseHostPills"', html)
            self.assertIn('id="browseHostBadge"', html)

            # Strict CORS must remain enforced for /api/safelist
            cors_res = client.get("/api/safelist", headers={"Origin": "https://evil.example"})
            self.assertEqual(cors_res.status_code, 200)
            self.assertNotIn("Access-Control-Allow-Origin", cors_res.headers)
        finally:
            srv.shutdown()

        # Verify frontend JS wires Option 1 switcher + Nearby Browse button + URL safety
        app_js_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "js", "app.js")
        with open(app_js_path, "r", encoding="utf-8") as f:
            js = f.read()
        self.assertIn("function safeHostUrl(", js)
        self.assertIn("function renderBrowseHostSwitcher(", js)
        self.assertIn("nearby-browse-btn", js)


if __name__ == "__main__":
    unittest.main()
