"""
NearBeam Mesh & Cluster Management Layer.
Enables Leader-Follower auto-discovery, heartbeat monitoring,
and unified multi-host file transfer and clipboard coordination.
"""

import ipaddress
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


def is_valid_peer_ip(ip) -> bool:
    """Returns True only for valid IPv4/IPv6 literal addresses (prevents URL/XSS injection)."""
    if not isinstance(ip, str):
        return False
    stripped = ip.strip()
    if not stripped:
        return False
    try:
        ipaddress.ip_address(stripped)
        return True
    except ValueError:
        return False


def parse_valid_port(port, default: Optional[int] = 5000) -> Optional[int]:
    """Parses a TCP port in 1..65535; rejects booleans and out-of-range values."""
    if port is None:
        return default
    if isinstance(port, bool):
        return None
    try:
        parsed = int(port)
    except (TypeError, ValueError):
        return None
    if 1 <= parsed <= 65535:
        return parsed
    return None


def _format_host_url(ip: str, port: int) -> str:
    host = f"[{ip}]" if (":" in ip and not ip.startswith("[")) else ip
    return f"http://{host}:{port}"


def _clean_peer_name(name) -> str:
    raw = str(name or "Unknown Node").strip()
    for suffix in (" (This PC)", " (Leader)"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].strip()
    return raw or "Unknown Node"


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
            "raw_name": self.name,
            "ip": self.ip,
            "port": self.port,
            "url": _format_host_url(self.ip, self.port),
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
        self._missed_leader_heartbeats = 0
        self.leader_info: Optional[dict] = None
        self.nodes: dict[str, MeshNode] = {}
        self._peer_nodes: dict[str, MeshNode] = {}

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
                    # following whichever announced last — unless the current leader already missed a heartbeat.
                    current = self.leader_info
                    if (
                        current
                        and self._missed_leader_heartbeats == 0
                        and current.get("node_id") != other["node_id"]
                        and _rank(other) >= _rank(current)
                    ):
                        return
                    changed = not current or current.get("node_id") != other["node_id"]
                    self.leader_info = other
                    self._missed_leader_heartbeats = 0
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
            port = parse_valid_port(msg.get("port"), default=5000)
            if (
                is_leader
                and isinstance(node_id, str)
                and node_id.strip()
                and is_valid_peer_ip(ip)
                and port is not None
            ):
                self.register_node(MeshNode(
                    node_id=node_id.strip(),
                    name=_clean_peer_name(msg.get("name")),
                    ip=ip.strip(),
                    port=port,
                    role="node",
                    last_seen=time.time(),
                ))

    def _step_down(self, leader: dict):
        """Another leader outranks this one: become its node (server then stops advertising nearbeam.local)."""
        with self._lock:
            self.role = "node"
            self.leader_info = leader
            self._missed_leader_heartbeats = 0
            self.nodes.clear()
            self._peer_nodes.clear()
        print(f"  [Mesh] Another Leader found: {leader.get('name')} ({leader.get('ip')}). Joining it as a Secondary Node.")
        threading.Thread(target=self._register_with_leader, daemon=True).start()
        if self.on_role_change:
            self.on_role_change("node")
        if self.on_nodes_change:
            self.on_nodes_change(self.get_nodes_list())

    def _update_leader_metadata_locked(self, data: dict):
        """Keeps leader_info synchronized if a leader restarted on the same IP:port."""
        if not self.leader_info or not isinstance(data, dict):
            return
        if data.get("leader_id"):
            self.leader_info["node_id"] = data["leader_id"]
        if data.get("leader_name"):
            self.leader_info["name"] = data["leader_name"]
        if data.get("leader_started_at") is not None:
            self.leader_info["started_at"] = data["leader_started_at"]

    def _sync_cluster_peers(self, raw_nodes) -> None:
        """Updates cached sibling peers received from Leader register/heartbeat responses."""
        if not isinstance(raw_nodes, list):
            return
        now = time.time()
        changed = False
        with self._lock:
            if self.role != "node":
                return
            leader_id = self.leader_info.get("node_id") if self.leader_info else None
            leader_addr = (
                (str(self.leader_info.get("ip", "")).strip(), parse_valid_port(self.leader_info.get("port"), default=5000))
                if self.leader_info
                else None
            )
            self_addr = (str(self.ip or "").strip(), self.port)

            new_peers: dict[str, MeshNode] = {}
            for item in raw_nodes:
                if not isinstance(item, dict):
                    continue
                raw_id = item.get("id") or item.get("node_id")
                if not isinstance(raw_id, str) or not raw_id.strip():
                    continue
                nid = raw_id.strip()
                raw_ip = item.get("ip")
                port = parse_valid_port(item.get("port"), default=None)
                if not is_valid_peer_ip(raw_ip) or port is None:
                    continue
                ip = raw_ip.strip()
                if nid == self.node_id or (leader_id and nid == leader_id):
                    continue
                if (ip, port) == self_addr or (leader_addr and (ip, port) == leader_addr):
                    continue
                name = _clean_peer_name(item.get("raw_name") or item.get("name"))
                new_peers[nid] = MeshNode(
                    node_id=nid,
                    name=name,
                    ip=ip,
                    port=port,
                    role="node",
                    last_seen=now,
                )

            old_sig = {k: (v.name, v.ip, v.port, v.role) for k, v in self._peer_nodes.items()}
            new_sig = {k: (v.name, v.ip, v.port, v.role) for k, v in new_peers.items()}
            if new_sig != old_sig:
                self._peer_nodes = new_peers
                changed = True
            else:
                for nid, peer in new_peers.items():
                    if nid in self._peer_nodes:
                        self._peer_nodes[nid].last_seen = now

        if changed and self.on_nodes_change:
            self.on_nodes_change(self.get_nodes_list())

    def _handle_not_leader_response(self, err_data: dict, allow_redirect: bool = True):
        """Re-anchors a secondary node if its target stepped down to non-leader."""
        redirect = err_data.get("leader") if isinstance(err_data, dict) else None
        if (
            allow_redirect
            and isinstance(redirect, dict)
            and redirect.get("ip")
            and redirect.get("port")
            and redirect.get("node_id") != self.node_id
        ):
            with self._lock:
                self.leader_info = {
                    "node_id": redirect.get("node_id"),
                    "name": redirect.get("name", "Primary PC"),
                    "ip": redirect["ip"],
                    "port": redirect["port"],
                    "started_at": redirect.get("started_at") or 0,
                }
                self._missed_leader_heartbeats = 0
                self._peer_nodes.clear()
            self._register_with_leader(allow_redirect=False)
            return
        # No valid redirect target: clear stale leader and run discovery/promotion.
        with self._lock:
            self.leader_info = None
            self._missed_leader_heartbeats = 0
            self._peer_nodes.clear()
        if self.running and not self._stop_event.is_set():
            self._discover_and_elect()

    def _register_with_leader(self, allow_redirect: bool = True):
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
                    body = json.loads(resp.read() or b"{}")
                    if body.get("leader_role") and body.get("leader_role") != "leader":
                        self._handle_not_leader_response(body, allow_redirect=allow_redirect)
                        return
                    with self._lock:
                        self._update_leader_metadata_locked(body)
                        self._missed_leader_heartbeats = 0
                    self._sync_cluster_peers(body.get("nodes"))
                    print(f"  [Mesh] Registered successfully with Leader @ {leader['ip']}:{leader['port']}")
        except urllib.error.HTTPError as he:
            if he.code == 409:
                try:
                    err_body = json.loads(he.read() or b"{}")
                except Exception:
                    err_body = {}
                self._handle_not_leader_response(err_body, allow_redirect=allow_redirect)
            else:
                print(f"  [Mesh] Registration with Leader failed: {he}")
        except Exception as e:
            print(f"  [Mesh] Registration with Leader failed: {e}")

    def _heartbeat_loop(self):
        """Sends periodic heartbeats if Node, or cleans dead nodes if Leader."""
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
                            body = json.loads(resp.read() or b"{}")
                            if body.get("leader_role") and body.get("leader_role") != "leader":
                                self._handle_not_leader_response(body)
                                continue
                            with self._lock:
                                self._missed_leader_heartbeats = 0
                                self._update_leader_metadata_locked(body)
                            self._sync_cluster_peers(body.get("nodes"))
                            # Leader restarted or merged and doesn't know us: register again.
                            if body.get("updated") is False:
                                self._register_with_leader()
                except urllib.error.HTTPError as he:
                    if not self.running:
                        break
                    if he.code == 409:
                        try:
                            err_body = json.loads(he.read() or b"{}")
                        except Exception:
                            err_body = {}
                        self._handle_not_leader_response(err_body)
                        continue
                    with self._lock:
                        self._missed_leader_heartbeats += 1
                        missed = self._missed_leader_heartbeats
                    print(f"  [Mesh] Missed heartbeat to Leader ({missed}/5)")
                    if missed >= 5:
                        print("  [Mesh] Leader is unreachable! Promoting self to Primary Leader.")
                        with self._lock:
                            self._missed_leader_heartbeats = 0
                        self.promote_to_leader()
                except Exception:
                    if not self.running:
                        break
                    with self._lock:
                        self._missed_leader_heartbeats += 1
                        missed = self._missed_leader_heartbeats
                    print(f"  [Mesh] Missed heartbeat to Leader ({missed}/5)")

                    if missed >= 5:
                        print("  [Mesh] Leader is unreachable! Promoting self to Primary Leader.")
                        with self._lock:
                            self._missed_leader_heartbeats = 0
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
            self._peer_nodes.clear()

        # Announce leadership to the network
        self._send_broadcast(self._leader_announcement())

        if self.on_role_change:
            self.on_role_change("leader")
        if self.on_nodes_change:
            self.on_nodes_change(self.get_nodes_list())

    def register_node(self, node_or_dict) -> bool:
        """Adds or updates a node in the leader's registry."""
        with self._lock:
            if self.role != "leader":
                return False
            if isinstance(node_or_dict, dict):
                ip = node_or_dict.get("ip")
                port = parse_valid_port(node_or_dict.get("port"), default=5000)
                node_id = node_or_dict.get("node_id")
                if not isinstance(node_id, str) or not node_id.strip() or not is_valid_peer_ip(ip) or port is None:
                    return False
                node = MeshNode(
                    node_id=node_id.strip(),
                    name=_clean_peer_name(node_or_dict.get("name")),
                    ip=ip.strip(),
                    port=port,
                    role="node",
                    last_seen=time.time(),
                )
            else:
                node = node_or_dict
            self.nodes[node.node_id] = node

        if self.on_nodes_change:
            self.on_nodes_change(self.get_nodes_list())
        return True

    def get_cluster_snapshot(self) -> list[dict]:
        """Returns canonical cluster membership (clean hostnames, no self suffix) for peer sync."""
        now = time.time()
        with self._lock:
            snapshot = [{
                "id": self.node_id,
                "name": self.device_name,
                "raw_name": self.device_name,
                "ip": self.ip,
                "port": self.port,
                "url": _format_host_url(self.ip, self.port),
                "role": self.role,
                "is_leader": (self.role == "leader"),
                "last_seen": now,
            }]
            if self.role == "leader":
                for node in self.nodes.values():
                    snapshot.append(node.to_dict(is_self=False))
            return snapshot

    def update_heartbeat(self, node_id: str) -> bool:
        """Updates last_seen timestamp for an active node."""
        with self._lock:
            if self.role != "leader":
                return False
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
                "url": _format_host_url(self.ip, self.port),
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
                leader_id = self.leader_info.get("node_id", "leader")
                leader_ip = self.leader_info.get("ip") or ""
                leader_port = parse_valid_port(self.leader_info.get("port"), default=5000) or 5000
                leader_name = _clean_peer_name(self.leader_info.get("name", "Primary PC"))
                leader_entry = {
                    "id": leader_id,
                    "name": f"{leader_name} (Leader)",
                    "raw_name": leader_name,
                    "ip": leader_ip,
                    "port": leader_port,
                    "url": _format_host_url(leader_ip, leader_port),
                    "role": "leader",
                    "is_leader": True,
                    "is_self": False,
                    "last_seen": time.time(),
                }
                nodes_list.append(leader_entry)
                for peer in self._peer_nodes.values():
                    if peer.node_id in (self.node_id, leader_id):
                        continue
                    nodes_list.append(peer.to_dict(is_self=False))

            return nodes_list

    def note_remote_clipboard(self, text: str = "", image_base64: Optional[str] = None):
        """Records clipboard content received from a peer so re-copying an older item later isn't dropped."""
        if not text and not image_base64:
            return
        with self._lock:
            self._last_synced_clipboard_hash = hash((text or "", image_base64 or ""))

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
