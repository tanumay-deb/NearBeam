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

from core.network import get_hostname, get_local_ip

MESH_DISCOVERY_PORT = 5005
HEARTBEAT_INTERVAL = 3.0
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
        self.leader_info: Optional[dict] = None
        self.nodes: dict[str, MeshNode] = {}

        self.on_role_change = on_role_change
        self.on_nodes_change = on_nodes_change
        self.on_remote_clipboard = on_remote_clipboard

        self.running = False
        self.udp_sock: Optional[socket.socket] = None
        self.listener_thread: Optional[threading.Thread] = None
        self.heartbeat_thread: Optional[threading.Thread] = None
        self._last_synced_clipboard_hash = None
        self._lock = threading.Lock()

    def start(self):
        """Initializes mesh UDP discovery and performs leader election."""
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

        # Start heartbeat monitoring thread
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()

    def _discover_and_elect(self):
        """Broadcasts WHO_IS_LEADER and listens for existing leader announcements."""
        print("  [Mesh] Searching for existing NearBeam Leader on Wi-Fi...")
        leader_found = False
        start_time = time.time()

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
        while time.time() - start_time < 1.5:
            if self.leader_info and self.leader_info.get("node_id") != self.node_id:
                leader_found = True
                break
            time.sleep(0.1)

        if leader_found and self.leader_info:
            self.role = "node"
            print(f"  [Mesh] Found active Leader: {self.leader_info.get('name')} ({self.leader_info.get('ip')}:{self.leader_info.get('port')})")
            print(f"  [Mesh] Joining cluster as Secondary Node.")
            # Register with leader via HTTP
            self._register_with_leader()
            if self.on_role_change:
                self.on_role_change(self.role)
        else:
            self.role = "leader"
            print(f"  [Mesh] No existing Leader detected. Elected as Primary Host (Leader).")
            if self.on_role_change:
                self.on_role_change(self.role)

    def _send_broadcast(self, data: dict):
        """Sends a JSON UDP broadcast packet to the local subnet."""
        if not self.udp_sock:
            return
        payload = json.dumps(data).encode("utf-8")
        try:
            self.udp_sock.sendto(payload, ("255.255.255.255", MESH_DISCOVERY_PORT))
        except Exception:
            try:
                parts = self.ip.split(".")
                if len(parts) == 4:
                    subnet_broadcast = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                    self.udp_sock.sendto(payload, (subnet_broadcast, MESH_DISCOVERY_PORT))
            except Exception:
                pass
        # Also broadcast to loopback so multiple instances on same machine discover each other
        try:
            self.udp_sock.sendto(payload, ("127.0.0.1", MESH_DISCOVERY_PORT))
        except Exception:
            pass

    def _listen_udp_loop(self):
        """Listens for peer beacons and responds to discovery requests."""
        while self.running and self.udp_sock:
            try:
                data, addr = self.udp_sock.recvfrom(4096)
                msg = json.loads(data.decode("utf-8"))
                msg_type = msg.get("type")

                # Ignore our own messages
                if msg.get("node_id") == self.node_id:
                    continue

                if msg_type == "WHO_IS_LEADER":
                    if self.role == "leader":
                        # Reply announcing that we are the Leader
                        reply = {
                            "type": "LEADER_ANNOUNCE",
                            "node_id": self.node_id,
                            "name": self.device_name,
                            "ip": self.ip,
                            "port": self.port,
                        }
                        payload = json.dumps(reply).encode("utf-8")
                        try:
                            self.udp_sock.sendto(payload, addr)
                        except Exception:
                            pass
                        self._send_broadcast(reply)

                elif msg_type == "LEADER_ANNOUNCE":
                    with self._lock:
                        if self.role != "leader" or msg.get("node_id") != self.node_id:
                            self.leader_info = {
                                "node_id": msg.get("node_id"),
                                "name": msg.get("name"),
                                "ip": msg.get("ip"),
                                "port": msg.get("port"),
                            }

                elif msg_type == "NODE_ANNOUNCE":
                    if self.role == "leader":
                        # Record node
                        node = MeshNode(
                            node_id=msg.get("node_id"),
                            name=msg.get("name"),
                            ip=msg.get("ip"),
                            port=msg.get("port"),
                            role="node",
                            last_seen=time.time(),
                        )
                        self.register_node(node)

            except socket.timeout:
                continue
            except Exception:
                if not self.running:
                    break

    def _register_with_leader(self):
        """Sends HTTP registration request to Primary Leader."""
        if not self.leader_info:
            return
        target_url = f"http://{self.leader_info['ip']}:{self.leader_info['port']}/api/mesh/register"
        payload = json.dumps({
            "node_id": self.node_id,
            "name": self.device_name,
            "ip": self.ip,
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
                    print(f"  [Mesh] Registered successfully with Leader @ {self.leader_info['ip']}:{self.leader_info['port']}")
        except Exception as e:
            print(f"  [Mesh] Registration with Leader failed: {e}")

    def _heartbeat_loop(self):
        """Sends periodic heartbeats if Node, or cleans dead nodes if Leader."""
        missed_leader_heartbeats = 0

        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)

            if self.role == "node" and self.leader_info:
                # Send heartbeat to leader
                hb_url = f"http://{self.leader_info['ip']}:{self.leader_info['port']}/api/mesh/heartbeat"
                payload = json.dumps({"node_id": self.node_id}).encode("utf-8")
                try:
                    req = urllib.request.Request(
                        hb_url,
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=2.5) as resp:
                        if resp.status == 200:
                            missed_leader_heartbeats = 0
                except Exception:
                    missed_leader_heartbeats += 1
                    print(f"  [Mesh] Missed heartbeat to Leader ({missed_leader_heartbeats}/5)")

                    if missed_leader_heartbeats >= 5:
                        print("  [Mesh] Leader is unreachable! Promoting self to Primary Leader.")
                        self.promote_to_leader()

            elif self.role == "leader":
                # Clean up inactive nodes
                self._prune_stale_nodes()

    def promote_to_leader(self):
        """Promotes this node to Primary Host (Leader)."""
        with self._lock:
            self.role = "leader"
            self.leader_info = None

        # Announce leadership to the network
        announcement = {
            "type": "LEADER_ANNOUNCE",
            "node_id": self.node_id,
            "name": self.device_name,
            "ip": self.ip,
            "port": self.port,
        }
        self._send_broadcast(announcement)

        if self.on_role_change:
            self.on_role_change(self.role)
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

    def broadcast_clipboard_mesh(self, text: str, source_node_id: Optional[str] = None):
        """Propagates clipboard text across all connected nodes in the cluster."""
        if not text:
            return

        text_hash = hash(text)
        if text_hash == self._last_synced_clipboard_hash:
            return
        self._last_synced_clipboard_hash = text_hash

        def _send_to_url(url: str, payload_dict: dict):
            try:
                data = json.dumps(payload_dict).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=2.0)
            except Exception:
                pass

        if self.role == "leader":
            # Propagate to all registered secondary nodes
            with self._lock:
                target_nodes = [n for nid, n in self.nodes.items() if nid != source_node_id]

            for node in target_nodes:
                endpoint = f"http://{node.ip}:{node.port}/api/mesh/clipboard-sync"
                threading.Thread(
                    target=_send_to_url,
                    args=(endpoint, {"text": text, "source_node_id": self.node_id}),
                    daemon=True,
                ).start()

        elif self.role == "node" and self.leader_info:
            # Propagate to leader
            endpoint = f"http://{self.leader_info['ip']}:{self.leader_info['port']}/api/mesh/clipboard-sync"
            threading.Thread(
                target=_send_to_url,
                args=(endpoint, {"text": text, "source_node_id": self.node_id}),
                daemon=True,
            ).start()

    def stop(self):
        """Stops background threads and closes sockets."""
        self.running = False
        if self.udp_sock:
            try:
                self.udp_sock.close()
            except Exception:
                pass
