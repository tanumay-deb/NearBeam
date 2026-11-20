"""
NearBeam Version and Update Management.
Checks for updates against GitHub Releases API.
"""

import json
import re
import urllib.error
import urllib.request

APP_VERSION = "1.6.3"
APP_NAME = "NearBeam"
GITHUB_REPO = "tanumay-deb/NearBeam"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def parse_version_tuple(v_str: str) -> tuple:
    """Parses version strings like 'v1.1.0' or 'v1.6.3-rc1' into (1, 6, 3)."""
    if not isinstance(v_str, str) or not v_str.strip():
        return (0, 0, 0)
    match = re.match(r"^[vV]?(\d+(?:\.\d+)*)", v_str.strip())
    if not match:
        return (0, 0, 0)
    parts = [int(p) for p in match.group(1).split(".")]
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
                "User-Agent": f"NearBeam-Client/{APP_VERSION}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                if not isinstance(data, dict):
                    data = {}
                tag_name = data.get("tag_name") or ""
                if not isinstance(tag_name, str):
                    tag_name = str(tag_name)
                remote_tuple = parse_version_tuple(tag_name)

                result["latest_version"] = tag_name.lstrip("vV") or APP_VERSION
                result["release_url"] = data.get("html_url") or f"{RELEASES_URL}/latest"
                result["release_title"] = data.get("name") or f"NearBeam {tag_name}"
                result["release_notes"] = data.get("body") or ""

                if remote_tuple > current_tuple:
                    result["update_available"] = True
    except Exception as e:
        result["error"] = str(e)
        result["checked"] = False

    return result
