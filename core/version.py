"""
LANDrop Version and Update Management.
Checks for updates against GitHub Releases API.
"""

import json
import re
import urllib.error
import urllib.request

APP_VERSION = "1.3.1"
APP_NAME = "LANDrop"
GITHUB_REPO = "tanumay-deb/LANDrop"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def parse_version_tuple(v_str: str) -> tuple:
    """Parses version strings like 'v1.1.0' or '1.0.0' into (1, 1, 0)."""
    if not v_str:
        return (0, 0, 0)
    cleaned = re.sub(r"[^\d.]", "", v_str.strip())
    parts = []
    for part in cleaned.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_for_updates(timeout_sec: float = 4.0) -> dict:
    """
    Queries GitHub API to check if a newer release exists.
    Returns a dict with update status, latest version, and release URL.
    """
    current_tuple = parse_version_tuple(APP_VERSION)
    result = {
        "current_version": APP_VERSION,
        "latest_version": APP_VERSION,
        "update_available": False,
        "release_url": f"{RELEASES_URL}/latest",
        "release_title": "",
        "release_notes": "",
        "checked": True,
        "error": None,
    }

    try:
        req = urllib.request.Request(
            LATEST_RELEASE_API,
            headers={
                "User-Agent": f"LANDrop-Client/{APP_VERSION}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                tag_name = data.get("tag_name", "")
                remote_tuple = parse_version_tuple(tag_name)

                result["latest_version"] = tag_name.lstrip("v")
                result["release_url"] = data.get("html_url", f"{RELEASES_URL}/latest")
                result["release_title"] = data.get("name", f"LANDrop {tag_name}")
                result["release_notes"] = data.get("body", "")

                if remote_tuple > current_tuple:
                    result["update_available"] = True
    except Exception as e:
        result["error"] = str(e)
        result["checked"] = False

    return result
