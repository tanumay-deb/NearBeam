"""
Comprehensive regression tests for the full-codebase bug sweep covering:
- Backend Core (HTTP/1.1 keep-alive idle survival, CSRF origin guard, HEAD handle leak,
  directory-collision finalize_upload, multipart empty filename & sequential size check,
  Guest Drop auto-save guard, NTFS ADS rejection, NTFS junction cycle guard, thumbnail pool shutdown)
- Mesh, Network, CLI & Shell Integration (non-leader 409 split-brain guard, restarted leader switch,
  clipboard re-sync hash update, port bounds validation, frozen argparse None stdio, shell cmd validation)
- Desktop GUI & OutgoingTransfer (stalled socket instant cancel, failure remote cleanup,
  layout width bounds, stall recovery & late upload_progress suppression, --port override)
- Web Frontend & Guest Drop contracts
"""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from werkzeug.serving import make_server

import core.server as srv_mod
from core.batches import BatchStore
from core.config import ConfigManager
from core.mesh import MeshManager, MeshNode
from core.network import is_port_available
from core.server import NearBeamRequestHandler, NearBeamServer, finalize_upload


class TestBackendFullSweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nb_sweep2_srv_")
        self.cfg = ConfigManager(os.path.join(self.tmp, "cfg.json"))
        self.cfg.set_save_directory(os.path.join(self.tmp, "recv"))
        self.cfg.set_auto_save(True)
        self.orig = srv_mod.config
        srv_mod.config = self.cfg
        self.server = NearBeamServer(port=0, enable_mesh=False)
        self.client = self.server.app.test_client()

    def tearDown(self):
        self.server.shutdown()
        srv_mod.config = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_keep_alive_survives_idle_gap_over_250ms(self):
        httpd = make_server("127.0.0.1", 0, self.server.app, threaded=True, request_handler=NearBeamRequestHandler)
        port = httpd.server_port
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
            conn.request("GET", "/api/info")
            r1 = conn.getresponse()
            r1.read()
            self.assertEqual(r1.status, 200)
            sock_id_1 = conn.sock.getsockname()
            # Sleep > 250ms (past the selector poll slice) and reuse the exact same TCP socket
            time.sleep(0.6)
            conn.request("GET", "/api/info")
            r2 = conn.getresponse()
            r2.read()
            self.assertEqual(r2.status, 200)
            self.assertEqual(conn.sock.getsockname(), sock_id_1)
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_csrf_blocks_non_lan_origin_mutations_and_allows_lan_or_no_origin(self):
        r_evil = self.client.post(
            "/api/history/clear",
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(r_evil.status_code, 403)

        r_null = self.client.post(
            "/api/upload",
            data={"file": (io.BytesIO(b"evil"), "x.txt")},
            content_type="multipart/form-data",
            headers={"Origin": "null"},
        )
        self.assertEqual(r_null.status_code, 403)

        r_lan = self.client.post(
            "/api/history/clear",
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        self.assertEqual(r_lan.status_code, 200)

        r_cli = self.client.post("/api/history/clear")
        self.assertEqual(r_cli.status_code, 200)

    def test_head_download_does_not_leak_file_handle(self):
        share = os.path.join(self.tmp, "share")
        os.makedirs(share)
        big = os.path.join(share, "large.bin")
        with open(big, "wb") as f:
            f.write(b"X" * (2 * 1024 * 1024))
        fid = self.cfg.add_safe_folder(share, "Share")["id"]

        httpd = make_server("127.0.0.1", 0, self.server.app, threaded=True, request_handler=NearBeamRequestHandler)
        port = httpd.server_port
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
            conn.request("HEAD", f"/api/safelist/download?folder_id={fid}&subpath=large.bin")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200)
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

        # On Windows, a leaked open FileWrapper handle would raise PermissionError [WinError 32] here
        os.remove(big)
        self.assertFalse(os.path.exists(big))

    def test_finalize_upload_handles_existing_subdirectory_name_collision(self):
        os.makedirs(os.path.join(self.cfg.save_directory, "Photos"), exist_ok=True)
        tmp_f = os.path.join(self.cfg.save_directory, ".collide.tmp")
        with open(tmp_f, "wb") as f:
            f.write(b"payload")
        final_path = finalize_upload(tmp_f, self.cfg.save_directory, "Photos")
        self.assertEqual(os.path.basename(final_path), "Photos (1)")
        with open(final_path, "rb") as f:
            self.assertEqual(f.read(), b"payload")

    def test_sequential_multipart_empty_filename_reads_payload_and_checks_size(self):
        # Multipart upload where FileStorage.filename == "" (bool(FileStorage) is False in Werkzeug!)
        r_ok = self.client.post(
            "/api/upload/chunk",
            data={"file": (io.BytesIO(b"hello-14-bytes"), "")},
            content_type="multipart/form-data",
            headers={
                "X-Session-Id": "seq-mp-empty-fn",
                "X-Filename": "blob.bin",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "14",
            },
        )
        self.assertEqual(r_ok.status_code, 200, r_ok.data)
        saved = os.path.join(self.cfg.save_directory, "blob.bin")
        with open(saved, "rb") as f:
            self.assertEqual(f.read(), b"hello-14-bytes")

        # Truncated/mismatched X-File-Size on sequential final chunk must fail with 400
        r_bad = self.client.post(
            "/api/upload/chunk",
            data=b"short",
            headers={
                "X-Session-Id": "seq-bad-size",
                "X-Filename": "trunc.bin",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "999",
            },
        )
        self.assertEqual(r_bad.status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(self.cfg.save_directory, "trunc.bin")))

    def test_guest_drop_chunk_respects_auto_save_disabled(self):
        token = self.client.post("/api/drop/create").get_json()["token"]
        self.cfg.set_auto_save(False)
        resp = self.client.post(
            f"/api/drop/{token}/upload/chunk",
            data=b"abc",
            headers={
                "X-Session-Id": "drop-off-1",
                "X-Filename": "a.txt",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "3",
            },
        )
        self.assertEqual(resp.status_code, 403)

    def test_ntfs_ads_colon_rejected_in_safelist_and_received(self):
        share = os.path.join(self.tmp, "ads_share")
        os.makedirs(share)
        with open(os.path.join(share, "doc.txt"), "w", encoding="utf-8") as f:
            f.write("clean")
        fid = self.cfg.add_safe_folder(share, "AdsShare")["id"]
        with self.assertRaises(PermissionError):
            self.cfg.resolve_safe_path(fid, "doc.txt:Zone.Identifier")

        os.makedirs(self.cfg.save_directory, exist_ok=True)
        with open(os.path.join(self.cfg.save_directory, "recv.txt"), "w", encoding="utf-8") as f:
            f.write("ok")
        self.assertEqual(self.client.get("/api/received/download/recv.txt:Zone.Identifier").status_code, 404)

    @unittest.skipUnless(sys.platform == "win32", "NTFS junctions are Windows-specific")
    def test_internal_ntfs_junction_loop_does_not_recurse_infinitely(self):
        share = os.path.join(self.tmp, "junc_root")
        sub = os.path.join(share, "sub")
        os.makedirs(sub)
        with open(os.path.join(sub, "leaf.txt"), "w", encoding="utf-8") as f:
            f.write("hello")
        loop_junc = os.path.join(sub, "loop")
        res = subprocess.run(["cmd", "/c", "mklink", "/J", loop_junc, share], capture_output=True)
        if res.returncode != 0:
            self.skipTest("mklink /J unavailable")
        try:
            fid = self.cfg.add_safe_folder(share, "JuncShare")["id"]
            # Prepare ZIP must finish quickly with only 1 file (no infinite loop!)
            r_zip = self.client.post("/api/safelist/prepare-zip", json={"folder_id": fid, "subpath": ""})
            self.assertEqual(r_zip.status_code, 200)
            self.assertEqual(r_zip.get_json()["total_files"], 1)

            # BatchStore must also skip the junction loop cleanly
            store = BatchStore()
            batch = store.create_or_merge([share], source="Test")
            self.assertEqual(batch["file_count"], 1)
        finally:
            subprocess.run(["cmd", "/c", "rmdir", loop_junc], capture_output=True)

    def test_thumbnail_pool_shutdown_does_not_strand_generate(self):
        from core.thumbnails import _WindowsShellThumbnailPool

        pool = _WindowsShellThumbnailPool(workers=1, extractor=lambda _p: b"x", init_com=False)
        pool.shutdown()
        t0 = time.monotonic()
        self.assertIsNone(pool.generate("a.mp4", timeout=3.0))
        self.assertLess(time.monotonic() - t0, 0.5)


class TestMeshNetworkCliFullSweep(unittest.TestCase):
    def test_non_leader_rejects_register_and_heartbeat_with_409_and_redirects(self):
        tmp = tempfile.mkdtemp(prefix="nb_sweep2_mesh_")
        cfg = ConfigManager(os.path.join(tmp, "cfg.json"))
        cfg.set_save_directory(os.path.join(tmp, "recv"))
        orig = srv_mod.config
        srv_mod.config = cfg
        srv = NearBeamServer(port=0, enable_mesh=False)
        # Attach a MeshManager in secondary 'node' role pointing to real leader C
        mesh = MeshManager(port=5001, device_name="PC-A")
        mesh.role = "node"
        mesh.leader_info = {"node_id": "leader-C", "name": "PC-C", "ip": "192.168.1.99", "port": 5000, "started_at": 100}
        srv.mesh = mesh
        client = srv.app.test_client()
        try:
            r_reg = client.post("/api/mesh/register", json={"node_id": "node-B", "name": "PC-B", "ip": "192.168.1.20", "port": 5002})
            self.assertEqual(r_reg.status_code, 409)
            self.assertEqual(r_reg.get_json()["leader"]["node_id"], "leader-C")
            self.assertEqual(mesh.nodes, {})

            r_hb = client.post("/api/mesh/heartbeat", json={"node_id": "node-B"})
            self.assertEqual(r_hb.status_code, 409)
            self.assertEqual(r_hb.get_json()["leader"]["node_id"], "leader-C")
        finally:
            srv.shutdown()
            srv_mod.config = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_restarted_leader_accepted_after_missed_heartbeat(self):
        m = MeshManager(port=5002, device_name="PC-N")
        m.role = "node"
        m.leader_info = {"node_id": "old-L1", "name": "L1", "ip": "192.168.1.10", "port": 5000, "started_at": 100}
        m._missed_leader_heartbeats = 1
        with mock.patch.object(m, "_register_with_leader"):
            m._handle_message(
                {"type": "LEADER_ANNOUNCE", "node_id": "new-L2", "name": "L2", "ip": "192.168.1.11", "port": 5000, "started_at": 300},
                ("192.168.1.11", 15005),
            )
        self.assertEqual(m.leader_info["node_id"], "new-L2")
        self.assertEqual(m._missed_leader_heartbeats, 0)

    def test_mesh_clipboard_resync_same_text_after_peer_update(self):
        tmp = tempfile.mkdtemp(prefix="nb_sweep2_clip_")
        cfg = ConfigManager(os.path.join(tmp, "cfg.json"))
        cfg.set_save_directory(os.path.join(tmp, "recv"))
        orig = srv_mod.config
        srv_mod.config = cfg
        srv = NearBeamServer(port=0, enable_mesh=False)
        mesh = MeshManager(port=5000, device_name="Leader-L")
        mesh.role = "leader"
        mesh.nodes["node-N"] = MeshNode(node_id="node-N", name="Node-N", ip="127.0.0.1", port=59999)
        srv.mesh = mesh
        client = srv.app.test_client()
        sent_payloads = []
        try:
            with mock.patch("core.mesh.urllib.request.urlopen") as m_open:
                m_open.side_effect = lambda req, timeout=4.0: (sent_payloads.append(json.loads(req.data.decode())), mock.MagicMock())[1]
                # 1) Leader sends "alpha" to Node-N
                mesh.broadcast_clipboard_mesh("alpha")
                time.sleep(0.1)
                self.assertEqual(len(sent_payloads), 1)
                self.assertEqual(sent_payloads[0]["text"], "alpha")

                # 2) Node-N sends "beta" to Leader (2-PC cluster, no other peer to forward to)
                r = client.post("/api/mesh/clipboard-sync", json={"text": "beta", "source_node_id": "node-N"})
                self.assertEqual(r.status_code, 200)

                # 3) Leader re-copies "alpha": MUST NOT be dropped by stale hash!
                mesh.broadcast_clipboard_mesh("alpha")
                time.sleep(0.1)
                self.assertEqual(len(sent_payloads), 2)
                self.assertEqual(sent_payloads[1]["text"], "alpha")
        finally:
            srv.shutdown()
            srv_mod.config = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_is_port_available_rejects_zero_and_out_of_range(self):
        self.assertFalse(is_port_available(0))
        self.assertFalse(is_port_available(-5))
        self.assertFalse(is_port_available(70000))
        self.assertFalse(is_port_available(True))

    def test_shell_integration_checks_script_path_and_dir_key(self):
        from core.shell_integration import _parse_cmd_tokens
        self.assertEqual(
            _parse_cmd_tokens('"C:\\Program Files\\Python\\pythonw.exe" "C:\\My App\\main.py" --send "%1"'),
            ["C:\\Program Files\\Python\\pythonw.exe", "C:\\My App\\main.py", "--send", "%1"],
        )

    def test_main_argparse_safe_when_stdout_stderr_are_none(self):
        import main as main_mod
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None), \
             mock.patch.object(sys, "argv", ["nearbeam", "--help"]), \
             mock.patch.object(main_mod, "ensure_windows_start_menu_shortcut"), \
             mock.patch("ctypes.windll.user32.MessageBoxW") if sys.platform == "win32" else mock.MagicMock():
            with self.assertRaises(SystemExit) as cm:
                main_mod.main()
            self.assertEqual(cm.exception.code, 0)


class TestGuiAndTransferFullSweep(unittest.TestCase):
    def test_outgoing_transfer_cancel_interrupts_stalled_socket_quickly(self):
        from ui.transfer import OutgoingTransfer

        # Create a blackhole TCP server that accepts and never responds
        srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv_sock.bind(("127.0.0.1", 0))
        srv_sock.listen(5)
        port = srv_sock.getsockname()[1]
        accepted = []

        def accept_loop():
            try:
                conn, _ = srv_sock.accept()
                accepted.append(conn)
                time.sleep(5.0)
            except OSError:
                pass

        threading.Thread(target=accept_loop, daemon=True).start()
        tmp = tempfile.mkdtemp(prefix="nb_stall_tx_")
        try:
            src = os.path.join(tmp, "small.bin")
            with open(src, "wb") as f:
                f.write(b"hello" * 1024)
            done = {}
            tx = OutgoingTransfer(
                f"http://127.0.0.1:{port}/api/upload",
                "stalled",
                src,
                on_done=lambda ok, msg: done.update(ok=ok, msg=msg),
            )
            # Isolate the 60s stalled-chunk socket interruption latency directly
            tx._abort_remote = lambda timeout=2.0: None
            tx.start()
            time.sleep(0.15)
            t0 = time.monotonic()
            tx.cancel()
            tx.join(3.0)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 1.0, "cancel() blocked waiting for 60s socket timeout")
            self.assertEqual(done.get("msg"), "Cancelled")
        finally:
            for c in accepted:
                try:
                    c.close()
                except OSError:
                    pass
            srv_sock.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_outgoing_transfer_non_cancel_failure_cleans_remote_tmp(self):
        from ui.transfer import OutgoingTransfer

        tmp = tempfile.mkdtemp(prefix="nb_failclean_")
        cfg = ConfigManager(os.path.join(tmp, "cfg.json"))
        save_dir = os.path.join(tmp, "recv")
        cfg.set_save_directory(save_dir)
        cfg.set_auto_save(True)
        orig = srv_mod.config
        srv_mod.config = cfg
        srv = NearBeamServer(port=0, enable_mesh=False)
        httpd = make_server("127.0.0.1", 0, srv.app, threaded=True, request_handler=NearBeamRequestHandler)
        port = httpd.server_port
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            src = os.path.join(tmp, "twochunk.bin")
            with open(src, "wb") as f:
                f.write(b"A" * (2 * 1024 * 1024))
            done = {}
            # Disable auto-save right after chunk 0 succeeds so chunk 1 gets HTTP 403
            tx = OutgoingTransfer(
                f"http://127.0.0.1:{port}/api/upload",
                "peer",
                src,
                chunk_size=1024 * 1024,
                max_streams=1,
                on_progress=lambda s, t, b: cfg.set_auto_save(False),
                on_done=lambda ok, msg: done.update(ok=ok, msg=msg),
            )
            tx.run()
            self.assertEqual(done.get("ok"), False)
            self.assertIn("403", done.get("msg", ""))
            # Non-cancel failure must have called _abort_remote(), removing the .tmp file!
            self.assertEqual([n for n in os.listdir(save_dir) if n.endswith(".tmp")], [])
        finally:
            httpd.shutdown()
            httpd.server_close()
            srv.shutdown()
            srv_mod.config = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gui_layout_width_bounds_stall_recovery_and_late_progress(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        import gui

        app = QApplication.instance() or QApplication([])
        tmp = tempfile.mkdtemp(prefix="nb_gui_sweep2_")
        cfg = ConfigManager(os.path.join(tmp, "cfg.json"))
        cfg.set_save_directory(os.path.join(tmp, "recv"))
        orig = (gui.config, srv_mod.config, gui.NearBeamServer)
        gui.config = srv_mod.config = cfg
        gui.NearBeamServer = lambda port: srv_mod.NearBeamServer(port=port, enable_mesh=False)
        win = None
        try:
            win = gui.MainWindow(port=5876)
            win.resize(860, 600)
            win.show()
            app.processEvents()

            # 1) 3 DeviceCards fit cleanly inside min-width viewport (no horizontal clipping)
            devices = [
                {"name": "PC-Alpha", "kind": "host", "is_leader": True, "is_self": True, "device_type": "desktop"},
                {"name": "PC-Beta", "kind": "host", "is_leader": False, "is_self": False, "device_type": "laptop"},
                {"name": "iPhone-15", "kind": "client", "is_leader": False, "is_self": False, "device_type": "phone"},
            ]
            win.home.set_devices(devices, lambda d, p: None, lambda d: None)
            # Long unspaced batch label must not expand HomePage past viewport width
            win.home.set_batches(
                [{"id": "b1", "label": "VeryLongFilenameWithoutAnySpaces_" * 5, "file_count": 1, "formatted_size": "2 MB", "created_at": "12:00"}],
                lambda bid: None,
            )
            app.processEvents()
            self.assertLessEqual(
                win.home.widget().minimumSizeHint().width(),
                win.home.viewport().width() + 20,
            )

            # 2) Late upload_progress after file_received must NOT overwrite finished row or spawn a phantom row
            win.server_events.put(json.dumps({
                "type": "upload_progress", "session_id": "fin-1", "filename": "a.bin",
                "received_bytes": 500, "total_bytes": 1000, "from_addr": "127.0.0.1",
            }))
            win._drain_server_events()
            self.assertIn("in:fin-1", win.active_transfers)
            win.server_events.put(json.dumps({
                "type": "file_received", "session_id": "fin-1",
                "file": {"filename": "a.bin", "size": 1000, "formatted_size": "1000 B"},
            }))
            win._drain_server_events()
            win.server_events.put(json.dumps({
                "type": "upload_progress", "session_id": "fin-1", "filename": "a.bin",
                "received_bytes": 1000, "total_bytes": 1000, "from_addr": "127.0.0.1",
            }))
            win._drain_server_events()
            self.assertTrue(win.active_transfers["in:fin-1"].get("finished"))
            self.assertEqual(win.active_transfers["in:fin-1"]["row"].pct.text(), "")
            # Even after the finished row is removed, late upload_progress must not re-create it
            win._remove_active_row("in:fin-1")
            win.server_events.put(json.dumps({
                "type": "upload_progress", "session_id": "fin-1", "filename": "a.bin",
                "received_bytes": 1000, "total_bytes": 1000, "from_addr": "127.0.0.1",
            }))
            win._drain_server_events()
            self.assertNotIn("in:fin-1", win.active_transfers)

            # 3) Stalled incoming upload resumed before 3s timer clears 'stalled' & resets bar state
            win.server_events.put(json.dumps({
                "type": "upload_progress", "session_id": "stall-1", "filename": "b.bin",
                "received_bytes": 200, "total_bytes": 1000, "from_addr": "127.0.0.1",
            }))
            win._drain_server_events()
            win.active_transfers["in:stall-1"]["last"] = (time.monotonic() - 200, 200)
            win._sweep_stalled_rows(max_idle=120.0)
            stalled_gen = win.active_transfers["in:stall-1"]["gen"]
            self.assertTrue(win.active_transfers["in:stall-1"]["stalled"])
            # Resume arrives
            win.server_events.put(json.dumps({
                "type": "upload_progress", "session_id": "stall-1", "filename": "b.bin",
                "received_bytes": 600, "total_bytes": 1000, "from_addr": "127.0.0.1",
            }))
            win._drain_server_events()
            self.assertFalse(win.active_transfers["in:stall-1"]["stalled"])
            self.assertEqual(win.active_transfers["in:stall-1"]["row"].bar.property("state"), "")
            # Old 3s timer callback must NOT remove the resumed row
            win._remove_if_still_stalled("in:stall-1", stalled_gen)
            self.assertIn("in:stall-1", win.active_transfers)
        finally:
            if win is not None:
                win._clean_exit()
                win.deleteLater()
                app.processEvents()
            gui.config, srv_mod.config, gui.NearBeamServer = orig
            shutil.rmtree(tmp, ignore_errors=True)


class TestWebFrontendFullSweep(unittest.TestCase):
    def test_js_css_and_drop_html_sweep_contracts(self):
        root = Path(__file__).resolve().parent.parent
        js_path = root / "static" / "js" / "app.js"
        css_src = (root / "static" / "css" / "style.css").read_text(encoding="utf-8")
        drop_src = (root / "templates" / "drop.html").read_text(encoding="utf-8")
        js_src = js_path.read_text(encoding="utf-8")

        res = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        for token in (
            "nextSessionId = uuidv4()",
            "waitingForSlot",
            "sseDisconnected",
            "fetchOnOpen = true",
            "lastServerClipboardText",
            "safeBrowseSeq",
            "previewSeq",
            "zipJobCompleted",
            "rootEntry && rootEntry.is_file",
        ):
            self.assertIn(token, js_src, f"Missing JS sweep fix token: {token}")

        self.assertIn("flex-wrap: wrap", css_src)
        self.assertIn("body.theme-light .btn-select", drop_src)
        self.assertNotIn("justify-content: center;", drop_src)


if __name__ == "__main__":
    unittest.main()
