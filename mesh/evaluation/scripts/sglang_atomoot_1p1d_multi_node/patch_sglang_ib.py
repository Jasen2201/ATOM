"""Patch SGLang's _validate_ib_devices to support JSON file/dict IB device maps.

Compatible with:
  - v0.5.9 (uses "Check for duplicates" with raise ValueError)
  - main/latest (uses "Deduplicate while preserving order" with logger.warning)

The patch inserts JSON file path and JSON dict string support before the
plain comma-separated parsing, so the rest of the validation is untouched.
"""
import sys

filepath = sys.argv[1] if len(sys.argv) > 1 else '/app/sglang/python/sglang/srt/server_args.py'
with open(filepath, 'r') as f:
    content = f.read()

# The anchor line that exists in ALL versions
ANCHOR = '        # Strip whitespace from device names\n        devices = [d.strip() for d in device_str.split(",") if d.strip()]'

# Idempotency: skip if already patched
if 'endswith(".json")' in content:
    print("Already patched")
    sys.exit(0)

if ANCHOR not in content:
    print("ERROR: anchor pattern not found and not already patched", file=sys.stderr)
    sys.exit(1)

JSON_SUPPORT = """        device_str = device_str.strip()

        # Check if it's a JSON file path
        if device_str.endswith(".json"):
            if not os.path.isfile(device_str):
                raise ValueError(f"IB device JSON file not found: {device_str}")
            import json as _json
            with open(device_str, "r") as f:
                mapping = _json.load(f)
            all_devices = set()
            for v in mapping.values():
                for d in v.split(","):
                    all_devices.add(d.strip())
            ib_sysfs_path = "/sys/class/infiniband"
            if os.path.isdir(ib_sysfs_path):
                available_devices = set(os.listdir(ib_sysfs_path))
                invalid = [d for d in all_devices if d not in available_devices]
                if invalid:
                    raise ValueError(
                        f"Invalid IB devices in JSON: {invalid}. "
                        f"Available: {sorted(available_devices)}"
                    )
            return device_str

        # Check if it's a JSON dict string
        try:
            import json as _json
            parsed = _json.loads(device_str)
            if isinstance(parsed, dict):
                return device_str
        except Exception:
            pass

"""

content = content.replace(ANCHOR, JSON_SUPPORT + ANCHOR, 1)

with open(filepath, 'w') as f:
    f.write(content)
print("Patched successfully")
