import sys

filepath = '/app/sglang/python/sglang/srt/server_args.py'
with open(filepath, 'r') as f:
    content = f.read()

old = '''    def _validate_ib_devices(self, device_str: str) -> Optional[str]:
        """
        Validate IB devices before passing to mooncake.

        Args:
            device_str: Comma-separated IB device names (e.g., "mlx5_0,mlx5_1")

        Returns:
            Normalized comma-separated string of validated device names, or None if input is None.
        """
        if device_str is None:
            logger.warning(
                "No IB devices specified for Mooncake backend, falling back to auto discovery."
            )
            return None

        # Strip whitespace from device names
        devices = [d.strip() for d in device_str.split(",") if d.strip()]
        if len(devices) == 0:
            raise ValueError("No valid IB devices specified")

        # Check for duplicates
        if len(devices) != len(set(devices)):
            raise ValueError(f"Duplicate IB devices specified: {device_str}")

        # Get available IB devices from sysfs
        ib_sysfs_path = "/sys/class/infiniband"
        if not os.path.isdir(ib_sysfs_path):
            raise RuntimeError(
                f"InfiniBand sysfs path not found: {ib_sysfs_path}. "
                "Please ensure InfiniBand drivers are installed."
            )

        available_devices = set(os.listdir(ib_sysfs_path))
        if len(available_devices) == 0:
            raise RuntimeError(f"No IB devices found in {ib_sysfs_path}")

        # Check for invalid devices
        invalid_devices = [d for d in devices if d not in available_devices]
        if len(invalid_devices) != 0:
            raise ValueError(
                f"Invalid IB devices specified: {invalid_devices}. "
                f"Available devices: {sorted(available_devices)}"
            )

        return ",".join(devices)'''

new = '''    def _validate_ib_devices(self, device_str: str) -> Optional[str]:
        """
        Validate IB devices before passing to mooncake.

        Supports:
        1. Comma-separated device names: "ionic_0,ionic_1"
        2. JSON file path: "/path/to/ib_map.json"
        3. JSON dict string: '{"0":"ionic_0","1":"ionic_1",...}'

        Returns:
            The original string (for JSON formats) or normalized comma-separated
            string (for plain format), or None if input is None.
        """
        if device_str is None:
            logger.warning(
                "No IB devices specified for Mooncake backend, falling back to auto discovery."
            )
            return None

        device_str = device_str.strip()

        # Check if it's a JSON file path
        if device_str.endswith(".json"):
            if not os.path.isfile(device_str):
                raise ValueError(f"IB device JSON file not found: {device_str}")
            # Validate all devices in the JSON
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
                all_devices = set()
                for v in parsed.values():
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
        except (ValueError, _json.JSONDecodeError):
            pass

        # Plain comma-separated format
        devices = [d.strip() for d in device_str.split(",") if d.strip()]
        if len(devices) == 0:
            raise ValueError("No valid IB devices specified")

        if len(devices) != len(set(devices)):
            raise ValueError(f"Duplicate IB devices specified: {device_str}")

        ib_sysfs_path = "/sys/class/infiniband"
        if not os.path.isdir(ib_sysfs_path):
            raise RuntimeError(
                f"InfiniBand sysfs path not found: {ib_sysfs_path}. "
                "Please ensure InfiniBand drivers are installed."
            )

        available_devices = set(os.listdir(ib_sysfs_path))
        if len(available_devices) == 0:
            raise RuntimeError(f"No IB devices found in {ib_sysfs_path}")

        invalid_devices = [d for d in devices if d not in available_devices]
        if len(invalid_devices) != 0:
            raise ValueError(
                f"Invalid IB devices specified: {invalid_devices}. "
                f"Available devices: {sorted(available_devices)}"
            )

        return ",".join(devices)'''

if old not in content:
    print("ERROR: old pattern not found", file=sys.stderr)
    sys.exit(1)

content = content.replace(old, new, 1)
with open(filepath, 'w') as f:
    f.write(content)
print("Patched successfully")
