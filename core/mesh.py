"""
NearBeam Mesh & Cluster Management Layer.
Enables Leader-Follower auto-discovery, heartbeat monitoring,
and unified multi-host file transfer and clipboard coordination.
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Optional

from core.network import get_all_local_ips, get_hostname, get_local_ip

MESH_DISCOVERY_PORT = 5005
HEARTBEAT_INTERVAL = 3.0


def _rank(info: dict) -> tuple:
    """Lower ranks first: the longest-running leader wins, node id breaks ties."""
    return (info.get("started_at") or 0, str(info.get("node_id")))
NODE_TIMEOUT = 30.0


class MeshNode:
    def __init__(
        self,
        node_id: str,
        name: str,
        ip: str,
        port: int,
        role: str = "node",
        last_seen: float = None,
    ):
        self.node_id = node_id
        self.name = name
        self.ip = ip
        self.port = port
        self.role = role
        self.last_seen = last_seen or time.time()

    def to_dict(self, is_self: bool = False) -> dict:
        return {
            "id": self.node_id,
            "name": self.name,
            "ip": self.ip,
            "port": self.port,
            "url": f"http://{self.ip}:{self.port}",
            "role": self.role,
            "is_leader": (self.role == "leader"),
            "is_self": is_self,
            "last_seen": self.last_seen,
        }


class MeshManager:
    """
    Manages zero-config peer discovery, role election (Leader vs Secondary Node),
    heartbeat monitoring, and multi-device cluster membership.
    """

    def __init__(
        self,
        port: int,
        device_name: Optional[str] = None,
        on_role_change: Optional[Callable[[str], None]] = None,
        on_nodes_change: Optional[Callable[[list], None]] = None,
        on_remote_clipboard: Optional[Callable[[str], None]] = None,
    ):
        self.port = port
        self.ip = get_local_ip()
        self.device_name = device_name or get_hostname()
        self.node_id = f"{self.device_name}_{self.port}_{uuid.uuid4().hex[:6]}"

        self.role = "leader"  # Will be determined during discovery
        self.started_at = time.time()  # older leader wins when two leaders meet
        self.electing = False
        self._beacon_count = 0
        self.leader_info: Optional[dict] = None
        self.nodes: dict[str, MeshNode] = {}

        self.on_role_change = on_role_change
        self.on_nodes_change = on_nodes_change
        self.on_remote_clipboard = on_remote_clipboard

        self.running = False
        self._stop_event = threading.Event()
        self.udp_sock: Optional[socket.socket] = None
        self.listener_thread: Optional[threading.Thread] = None
        self.heartbeat_thread: Optional[threading.Thread] = None
        self._last_synced_clipboard_hash = None
        self._lock = threading.Lock()

    def start(self):
        """Initializes mesh UDP discovery and performs leader election."""
        self._stop_event.clear()
        self.running = True
        self.ip = get_local_ip()

        # Initialize UDP socket for broadcast & listening
        try:
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # Try binding to 0.0.0.0:5005
            try:
                self.udp_sock.bind(("", MESH_DISCOVERY_PORT))
            except Exception:
                # If specific port binding fails, bind to any available port
                self.udp_sock.bind(("", 0))
            self.udp_sock.settimeout(0.5)
        except Exception as e:
            print(f"  [Mesh] UDP socket error: {e}. Defaulting to standalone Leader.")
            self.role = "leader"
            return

        # Start UDP listener thread
        self.listener_thread = threading.Thread(target=self._listen_udp_loop, daemon=True)
        self.listener_thread.start()

        # Perform discovery beacon
        self._discover_and_elect()

        if not self.running:
            return

        # Start heartbeat monitoring thread
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()

    def _discover_and_elect(self):
        """Broadcasts WHO_IS_LEADER and listens for existing leader announcements."""
        print("  [Mesh] Searching for existing NearBeam Leader on Wi-Fi...")
        start_time = time.time()
        with self._lock:
            self.electing = True

        # Send discovery beacon
        beacon = {
            "type": "WHO_IS_LEADER",
            "node_id": self.node_id,
            "name": self.device_name,
            "ip": self.ip,
            "port": self.port,
        }
        self._send_broadcast(beacon)

        # Wait up to 1.5 seconds for a Leader announcement
        while self.running and (time.time() - start_time < 1.5):
            with self._lock:
                if self.leader_info and self.leader_info.get("node_id") != self.node_id:
                    break
            if self._stop_event.wait(0.1):
                break

        with self._lock:
            self.electing = False
            if not self.running:
                return
            leader_found = bool(self.leader_info and self.leader_info.get("node_id") != self.node_id)
            if leader_found:
                self.role = "node"
                leader_snapshot = dict(self.leader_info)
            else:
                self.role = "leader"
                leader_snapshot = None
            role_snapshot = self.role

        if leader_snapshot:
            print(f"  [Mesh] Found active Leader: {leader_snapshot.get('name')} ({leader_snapshot.get('ip')}:{leader_snapshot.get('port')})")
            print(f"  [Mesh] Joining cluster as Secondary Node.")
            # Register with leader via HTTP
            self._register_with_leader()
            if self.on_role_change and self.running:
                self.on_role_change(role_snapshot)
        else:
            print(f"  [Mesh] No existing Leader detected. Elected as Primary Host (Leader).")
            if self.on_role_change and self.running:
                self.on_role_change(role_snapshot)

    def _send_broadcast(self, data: dict):
        """Sends a JSON UDP broadcast packet to the local subnet(s) and loopback."""
        sock = self.udp_sock
        if not sock:
            return
        payload = json.dumps(data).encode("utf-8")
        targets = [("255.255.255.255", MESH_DISCOVERY_PORT)]
        seen_bcast = {"255.255.255.255"}
        candidate_ips = list(get_all_local_ips())
        if self.ip and not self.ip.startswith("127."):
            candidate_ips.append(self.ip)
        for addr in candidate_ips:
            parts = addr.split(".")
            if len(parts) == 4:
                bcast = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                if bcast not in seen_bcast:
                    seen_bcast.add(bcast)
                    targets.append((bcast, MESH_DISCOVERY_PORT))
        # Also broadcast to loopback so multiple instances on same machine discover each other
        targets.append(("127.0.0.1", MESH_DISCOVERY_PORT))

        for target in targets:
            try:
                sock.sendto(payload, target)
            except Exception:
                pass

    def _leader_announcement(self) -> dict:
        return {
            "type": "LEADER_ANNOUNCE",
            "node_id": self.node_id,
            "name": self.device_name,
            "ip": self.ip,
            "port": self.port,
            "started_at": self.started_at,
        }

    def _listen_udp_loop(self):
        """Listens for peer beacons and responds to discovery requests."""
        while self.running:
            sock = self.udp_sock
            if not sock:
                break
            try:
                data, addr = sock.recvfrom(4096)
                msg = json.loads(data.decode("utf-8"))
                if isinstance(msg, dict):
                    self._handle_message(msg, addr)
            except socket.timeout:
                continue
            except Exception:
                if not self.running:
                    break

    def _handle_message(self, msg: dict, addr):
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("type")
        if msg.get("node_id") == self.node_id:
            return  # our own broadcast

        if msg_type == "WHO_IS_LEADER":
            with self._lock:
                should_reply = self.role == "leader" and not self.electing
            if should_reply:
                reply = self._leader_announcement()
                sock = self.udp_sock
                if sock and addr:
                    try:
                        sock.sendto(json.dumps(reply).encode("utf-8"), addr)
                    except Exception:
                        pass
                self._send_broadcast(reply)

        elif msg_type == "LEADER_ANNOUNCE":
            other = {k: msg.get(k) for k in ("node_id", "name", "ip", "port")}
            other["started_at"] = msg.get("started_at") or 0  # pre-1.6.2 leaders send none: they rank first
            step_down_target = None
            should_register = False
            with self._lock:
                if self.role == "leader" and not self.electing:
                    # Two leaders (a lost discovery packet, firewall, simultaneous start) would both
                    # advertise nearbeam.local and break it. The newer one steps down and joins.
                    if _rank(other) < _rank({"started_at": self.started_at, "node_id": self.node_id}):
                        step_down_target = other
                else:
                    # While leaders coexist, every node picks the same (oldest) one instead of
                    # following whichever announced last.
                    current = self.leader_info
                    if current and current.get("node_id") != other["node_id"] and _rank(other) >= _rank(current):
                        return
                    changed = not current or current.get("node_id") != other["node_id"]
                    self.leader_info = other
                    should_register = changed and not self.electing  # during election _discover_and_elect registers
            if step_down_target is not None:
                self._step_down(step_down_target)
                return
            if should_register:
                threading.Thread(target=self._register_with_leader, daemon=True).start()

        elif msg_type == "NODE_ANNOUNCE":
            with self._lock:
                is_leader = self.role == "leader"
            node_id = msg.get("node_id")
            ip = msg.get("ip")
            if is_leader and isinstance(node_id, str) and node_id and isinstance(ip, str) and ip:
                self.register_node(MeshNode(
                    node_id=node_id,
                    name=str(msg.get("name") or "Unknown Node"),
                    ip=ip,
                    port=int(msg.get("port") or 5000),
                    role="node",
                    last_seen=time.time(),
                ))

    def _step_down(self, leader: dict):
        """Another leader outranks this one: become its node (server then stops advertising nearbeam.local)."""
        with self._lock:
            self.role = "node"
            self.leader_info = leader
            self.nodes.clear()
        print(f"  [Mesh] Another Leader found: {leader.get('name')} ({leader.get('ip')}). Joining it as a Secondary Node.")
        threading.Thread(target=self._register_with_leader, daemon=True).start()
        if self.on_role_change:
            self.on_role_change("node")
        if self.on_nodes_change:
            self.on_nodes_change(self.get_nodes_list())

    def _register_with_leader(self):
        """Sends HTTP registration request to Primary Leader."""
        with self._lock:
            leader = dict(self.leader_info) if self.leader_info else None
            my_ip = self.ip
        if not leader or not leader.get("ip") or not leader.get("port"):
            return
        target_url = f"http://{leader['ip']}:{leader['port']}/api/mesh/register"
        payload = json.dumps({
            "node_id": self.node_id,
            "name": self.device_name,
            "ip": my_ip,
            "port": self.port,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                target_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    print(f"  [Mesh] Registered successfully with Leader @ {leader['ip']}:{leader['port']}")
        except Exception as e:
            print(f"  [Mesh] Registration with Leader failed: {e}")

    def _heartbeat_loop(self):
        """Sends periodic heartbeats if Node, or cleans dead nodes if Leader."""
        missed_leader_heartbeats = 0

        while self.running and not self._stop_event.wait(HEARTBEAT_INTERVAL):
            with self._lock:
                role = self.role
                leader = dict(self.leader_info) if self.leader_info else None

            if role == "node" and leader:
                # Send heartbeat to leader
                hb_url = f"http://{leader['ip']}:{leader['port']}/api/mesh/heartbeat"
                payload = json.dumps({"node_id": self.node_id}).encode("utf-8")
                try:
                    req = urllib.request.Request(
                        hb_url,
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=2.5) as resp:
                        if not self.running:
                            break
                        if resp.status == 200:
                            missed_leader_heartbeats = 0
                            # Leader restarted or merged and doesn't know us: register again.
                            if json.loads(resp.read() or b"{}").get("updated") is False:
                                self._register_with_leader()
                except Exception:
                    if not self.running:
                        break
                    missed_leader_heartbeats += 1
                    print(f"  [Mesh] Missed heartbeat to Leader ({missed_leader_heartbeats}/5)")

                    if missed_leader_heartbeats >= 5:
                        print("  [Mesh] Leader is unreachable! Promoting self to Primary Leader.")
                        missed_leader_heartbeats = 0  # else a later step-down re-promotes on its first miss
                        self.promote_to_leader()

            elif role == "leader":
                # Clean up inactive nodes
                self._prune_stale_nodes()
                self._leader_beacon()

    def _leader_beacon(self):
        """Leaders announce every heartbeat so a second leader (missed discovery) hears them and merges.
        Every 5th beat they also ask WHO_IS_LEADER: pre-1.6.2 leaders only speak when asked."""
        self._beacon_count += 1
        self._send_broadcast(self._leader_announcement())
        if self._beacon_count % 5 == 0:
            self._send_broadcast({
                "type": "WHO_IS_LEADER",
                "node_id": self.node_id,
                "name": self.device_name,
                "ip": self.ip,
                "port": self.port,
            })

    def set_ip(self, ip: str):
        """The PC moved networks: announcements carry the new address, and a node re-registers."""
        with self._lock:
            self.ip = ip
            should_reregister = self.role == "node" and bool(self.leader_info)
        if should_reregister:
            threading.Thread(target=self._register_with_leader, daemon=True).start()

    def promote_to_leader(self):
        """Promotes this node to Primary Host (Leader)."""
        if self._stop_event.is_set():
            return
        with self._lock:
            self.role = "leader"
            self.leader_info = None

        # Announce leadership to the network
        self._send_broadcast(self._leader_announcement())

        if self.on_role_change:
            self.on_role_change("leader")
        if self.on_nodes_change:
            self.on_nodes_change(self.get_nodes_list())

    def register_node(self, node_or_dict) -> bool:
        """Adds or updates a node in the leader's registry."""
        with self._lock:
            if isinstance(node_or_dict, dict):
                node = MeshNode(
                    node_id=node_or_dict["node_id"],
                    name=node_or_dict.get("name", "Unknown Node"),
                    ip=node_or_dict["ip"],
                    port=node_or_dict.get("port", 5000),
                    role="node",
                    last_seen=time.time(),
                )
            else:
                node = node_or_dict
            self.nodes[node.node_id] = node

        if self.on_nodes_change:
            self.on_nodes_change(self.get_nodes_list())
        return True

    def update_heartbeat(self, node_id: str) -> bool:
        """Updates last_seen timestamp for an active node."""
        with self._lock:
            if node_id in self.nodes:
                self.nodes[node_id].last_seen = time.time()
                return True
        return False

    def _prune_stale_nodes(self):
        """Removes nodes that have not reported in over NODE_TIMEOUT seconds."""
        now = time.time()
        stale_ids = []
        with self._lock:
            for nid, node in self.nodes.items():
                if now - node.last_seen > NODE_TIMEOUT:
                    stale_ids.append(nid)

            if stale_ids:
                for nid in stale_ids:
                    del self.nodes[nid]

        if stale_ids and self.on_nodes_change:
            self.on_nodes_change(self.get_nodes_list())

    def get_nodes_list(self) -> list[dict]:
        """Returns all currently active cluster nodes including self."""
        with self._lock:
            # Self entry
            self_entry = {
                "id": self.node_id,
                "name": f"{self.device_name} (This PC)",
                "raw_name": self.device_name,
                "ip": self.ip,
                "port": self.port,
                "url": f"http://{self.ip}:{self.port}",
                "role": self.role,
                "is_leader": (self.role == "leader"),
                "is_self": True,
                "last_seen": time.time(),
            }

            nodes_list = [self_entry]
            if self.role == "leader":
                for node in self.nodes.values():
                    nodes_list.append(node.to_dict(is_self=False))
            elif self.role == "node" and self.leader_info:
                # Add leader to the list for the secondary node's perspective
                leader_entry = {
                    "id": self.leader_info.get("node_id", "leader"),
                    "name": f"{self.leader_info.get('name', 'Primary PC')} (Leader)",
                    "raw_name": self.leader_info.get("name", "Primary PC"),
                    "ip": self.leader_info.get("ip"),
                    "port": self.leader_info.get("port", 5000),
                    "url": f"http://{self.leader_info.get('ip')}:{self.leader_info.get('port', 5000)}",
                    "role": "leader",
                    "is_leader": True,
                    "is_self": False,
                    "last_seen": time.time(),
                }
                nodes_list.append(leader_entry)

            return nodes_list

    def broadcast_clipboard_mesh(
        self,
        text: str = "",
        source_node_id: Optional[str] = None,
        image_base64: Optional[str] = None,
        image_mime: str = "image/png",
    ):
        """Propagates clipboard text or image across all connected nodes in the cluster."""
        if not text and not image_base64:
            return

        payload_hash = hash((text or "", image_base64 or ""))
        with self._lock:
            if self.role == "leader":
                endpoints = [
                    f"http://{n.ip}:{n.port}/api/mesh/clipboard-sync"
                    for nid, n in self.nodes.items()
                    if nid != source_node_id
                ]
            elif self.role == "node" and self.leader_info:
                endpoints = [
                    f"http://{self.leader_info['ip']}:{self.leader_info['port']}/api/mesh/clipboard-sync"
                ]
            else:
                endpoints = []

            if not endpoints:
                return
            if payload_hash == self._last_synced_clipboard_hash:
                return
            self._last_synced_clipboard_hash = payload_hash

        def _send_to_url(url: str, payload_dict: dict):
            try:
                data = json.dumps(payload_dict).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=4.0):
                    pass
            except Exception:
                pass

        payload_body = {
            "text": text or "",
            "source_node_id": self.node_id,
        }
        if image_base64:
            payload_body["image_base64"] = image_base64
            payload_body["image_mime"] = image_mime

        for endpoint in endpoints:
            threading.Thread(
                target=_send_to_url,
                args=(endpoint, payload_body),
                daemon=True,
            ).start()

    def stop(self):
        """Stops background threads and closes sockets."""
        self.running = False
        self._stop_event.set()
        sock = self.udp_sock
        self.udp_sock = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        current = threading.current_thread()
        for t in (self.listener_thread, self.heartbeat_thread):
            if t and t.is_alive() and t is not current:
                t.join(timeout=1.5)
