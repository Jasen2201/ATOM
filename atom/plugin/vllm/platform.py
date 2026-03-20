"""ATOM vLLM platform integration.

This module contains the vLLM `Platform` subclass used in ATOM's vLLM plugin
mode. Keep platform behavior here so `register.py` can focus on registration
and wiring only.
"""

import logging

from atom.utils import envs

logger = logging.getLogger("atom")
# This flag is used to enable the vLLM plugin mode.
disable_vllm_plugin = envs.ATOM_DISABLE_VLLM_PLUGIN
disable_vllm_plugin_attention = envs.ATOM_DISABLE_VLLM_PLUGIN_ATTENTION

if not disable_vllm_plugin:
    from vllm.platforms.rocm import RocmPlatform

    class ATOMPlatform(RocmPlatform):
        @classmethod
        def check_and_update_config(cls, vllm_config) -> None:
            super().check_and_update_config(vllm_config)
            # ATOM MLA attention backend only supports block_size=1.
            # RocmPlatform defaults to 16, which causes a mismatch in
            # Mooncake connector's register_kv_caches assertion.
            if (
                vllm_config.cache_config
                and vllm_config.model_config
                and vllm_config.model_config.use_mla
            ):
                vllm_config.cache_config.block_size = 1

        # For multi-modality models, to make AiterBackend supported by ViT,
        # get_supported_vit_attn_backends may need to be overridden here
        @classmethod
        def get_attn_backend_cls(
            cls, selected_backend, attn_selector_config, num_heads
        ) -> str:
            if disable_vllm_plugin_attention:
                logger.info("Fallback to original vLLM attention backend")
                return super().get_attn_backend_cls(
                    selected_backend, attn_selector_config, num_heads
                )

            logger.info("Use atom attention backend")
            if attn_selector_config.use_mla:
                return "atom.model_ops.attentions.aiter_mla.AiterMLABackend"
            return "atom.model_ops.attentions.aiter_attention.AiterBackend"

else:
    ATOMPlatform = None
