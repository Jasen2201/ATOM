from typing import Optional

from mesh_router.router_args import RouterArgs
from mesh_router.mesh_router_rs import (
    BackendType,
    PolicyType,
)
from mesh_router.mesh_router_rs import Router as _Router


def policy_from_str(policy_str: Optional[str]) -> PolicyType:
    """Convert policy string to PolicyType enum."""
    if policy_str is None:
        return None
    policy_map = {
        "random": PolicyType.Random,
        "round_robin": PolicyType.RoundRobin,
        "cache_aware": PolicyType.CacheAware,
        "power_of_two": PolicyType.PowerOfTwo,
        "prefix_hash": PolicyType.PrefixHash,
    }
    return policy_map[policy_str]


def backend_from_str(backend_str: Optional[str]) -> BackendType:
    """Convert backend string to BackendType enum."""
    if isinstance(backend_str, BackendType):
        return backend_str
    if backend_str is None:
        return BackendType.Sglang
    backend_map = {"sglang": BackendType.Sglang}
    backend_lower = backend_str.lower()
    if backend_lower not in backend_map:
        raise ValueError(
            f"Unknown backend: {backend_str}. Valid options: {', '.join(backend_map.keys())}"
        )
    return backend_map[backend_lower]


class Router:
    """
    A high-performance router for distributing requests across worker nodes.
    """

    def __init__(self, router: _Router):
        self._router = router

    @staticmethod
    def from_args(args: RouterArgs) -> "Router":
        """Create a router from a RouterArgs instance."""

        args_dict = vars(args)
        # Convert RouterArgs to _Router parameters
        args_dict["worker_urls"] = (
            []
            if args_dict["service_discovery"] or args_dict["pd_disaggregation"]
            else args_dict["worker_urls"]
        )
        args_dict["policy"] = policy_from_str(args_dict["policy"])
        args_dict["prefill_urls"] = (
            args_dict["prefill_urls"] if args_dict["pd_disaggregation"] else None
        )
        args_dict["decode_urls"] = (
            args_dict["decode_urls"] if args_dict["pd_disaggregation"] else None
        )
        args_dict["prefill_policy"] = policy_from_str(args_dict["prefill_policy"])
        args_dict["decode_policy"] = policy_from_str(args_dict["decode_policy"])

        # Convert backend
        args_dict["backend"] = backend_from_str(args_dict.get("backend"))

        # Remove fields that shouldn't be passed to Rust Router constructor
        # (deleted features, internal-only fields, or fields handled separately)
        fields_to_remove = [
            "mini_lb",
            "test_external_dp_routing",
            # Deleted: auth
            "control_plane_auth",
            "control_plane_api_keys",
            "control_plane_audit_enabled",
            "jwt_issuer",
            "jwt_audience",
            "jwt_jwks_uri",
            "jwt_role_mapping",
            # Deleted: TLS certs
            "client_cert_path",
            "client_key_path",
            "ca_cert_paths",
            "server_cert_path",
            "server_key_path",
            # Deleted: CORS, cloud, IGW
            "cors_allowed_origins",
            "history_backend",
            "enable_igw",
            # Deleted: Manual/Bucket strategy fields
            "max_idle_secs",
            "assignment_mode",
            "bucket_adjust_interval_secs",
            # Deleted: MCP
            "mcp_config_path",
            # Deleted: database backends
            "oracle_wallet_path",
            "oracle_tns_alias",
            "oracle_connect_descriptor",
            "oracle_username",
            "oracle_password",
            "oracle_pool_min",
            "oracle_pool_max",
            "oracle_pool_timeout_secs",
            "postgres_db_url",
            "postgres_pool_max",
            "redis_url",
            "redis_pool_max",
            "redis_retention_days",
            # Handled via backend_from_str or not needed
            "backend",
        ]
        for field in fields_to_remove:
            args_dict.pop(field, None)

        return Router(_Router(**args_dict))

    def start(self) -> None:
        """Start the router server.

        This method blocks until the server is shut down.
        """
        self._router.start()
