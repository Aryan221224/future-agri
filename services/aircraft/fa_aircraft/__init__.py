"""fa_aircraft: the console's ArduPilot back-end (MAVLink 2)."""
import os

# MAVLink 2 message definitions (mission_type for fences, extensions); must be set before
# pymavlink is imported anywhere in the process.
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("MAVLINK_DIALECT", "ardupilotmega")
