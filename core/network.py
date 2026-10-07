"""
Network utilities for local IP detection, mDNS hostname resolution, and port management.
"""

import io
import socket


def get_hostname() -> str:
    """Returns the local computer hostname."""
    try:
        return socket.gethostname()
    except Exception:
        return "localhost"


def get_local_ip() -> str:
    """
    Returns the primary non-loopback IPv4 address on the active network interface.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting to a public IP doesn't send packets, but prompts OS to select the active interface
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def get_all_local_ips() -> list[str]:
    """
    Enumerates all active non-loopback IPv4 addresses across all network adapters.
    """
    ips = set()
    primary = get_local_ip()
    if primary and not primary.startswith("127."):
        ips.add(primary)

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass

    return sorted(list(ips))


def is_port_available(port: int, host: str = "0.0.0.0") -> bool:
    """Checks if a given port is available for binding."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_available_port(start_port: int = 5000, max_attempts: int = 50) -> int:
    """Finds an available TCP port starting from start_port."""
    for port in range(start_port, start_port + max_attempts):
        if is_port_available(port):
            return port
    raise RuntimeError(f"No available port found in range {start_port} to {start_port + max_attempts}")


def get_connection_urls(port: int) -> dict:
    """
    Generates user-friendly connection URLs for the local network:
    - Permanent mDNS URL (e.g. http://nearbeam.local:5000)
    - Hostname mDNS URL (e.g. http://DESKTOP-2SC4QS1.local:5000)
    - IP URL (e.g. http://192.168.1.101:5000)
    """
    hostname = get_hostname()
    primary_ip = get_local_ip()
    all_ips = get_all_local_ips()

    nearbeam_url = f"http://nearbeam.local:{port}"
    mdns_url = f"http://{hostname}.local:{port}"
    primary_url = f"http://{primary_ip}:{port}"
    all_urls = [f"http://{ip}:{port}" for ip in all_ips]

    return {
        "hostname": hostname,
        "nearbeam_url": nearbeam_url,
        "landrop_url": nearbeam_url,  # Legacy alias
        "mdns_url": mdns_url,
        "primary_ip": primary_ip,
        "primary_url": primary_url,
        "all_urls": all_urls,
        "port": port,
    }


class ZeroconfBroadcaster:
    """
    Advertises mDNS service and hostnames (e.g. nearbeam.local) over the local Wi-Fi.
    Allows devices to find and connect to this PC even if its IP address changes.
    """
    def __init__(self, name: str = "nearbeam", port: int = 5000):
        self.name = name
        self.port = port
        self.zc = None
        self.info = None

    def start(self, ip: str = None):
        try:
            from zeroconf import ServiceInfo, Zeroconf
            target_ip = ip or get_local_ip()
            if target_ip.startswith("127."):
                return

            self.zc = Zeroconf()
            self.info = ServiceInfo(
                type_="_http._tcp.local.",
                name=f"{self.name}._http._tcp.local.",
                addresses=[socket.inet_aton(target_ip)],
                port=self.port,
                properties={"path": "/", "app": "NearBeam"},
                server=f"{self.name}.local.",
            )
            self.zc.register_service(self.info)
            print(f"  [Zeroconf] Broadcasting permanent address: http://{self.name}.local:{self.port}")
        except Exception as e:
            print(f"  [Zeroconf] Could not start mDNS broadcaster: {e}")

    def stop(self):
        if self.zc and self.info:
            try:
                self.zc.unregister_service(self.info)
                self.zc.close()
            except Exception:
                pass
            self.zc = None
            self.info = None


def generate_qr_png_bytes(url: str) -> bytes:
    """
    Generates a PNG image of a QR code pointing to the given URL.
    Returns raw PNG bytes.
    """
    try:
        import qrcode

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#1e293b", back_color="#ffffff")

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        print(f"QR generation error: {e}")
        return b""
