"""
Keep test clusters away from real NearBeam instances on the same Wi-Fi: a private
discovery port, and no nearbeam.local advertising. Without this, live mesh tests joined
real leaders (and pushed test clipboard text into them) and failed whenever one was running.
"""

import core.mesh
import core.network

core.mesh.MESH_DISCOVERY_PORT = 15005
core.network.ZeroconfBroadcaster.start = lambda self, ip=None: None
