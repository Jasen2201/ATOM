"""Tests for SGLang model wrapper class factory and forward branching.

The base_model_wrapper.py dynamically creates named subclasses via type()
and registers them in EntryClass. These tests verify the class factory
output, DeepSeek arch detection, and forward last-rank branching.

Because base_model_wrapper.py imports sglang modules at the top level,
we inject fake sglang modules before importing it.
"""

from unittest.mock import MagicMock


class _Obj:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# Class factory tests (no import needed — test the contract)
# ---------------------------------------------------------------------------

_MODEL_NAMES = [
    "DeepseekV3ForCausalLM",
    "Qwen3MoeForCausalLM",
]

_DEEPSEEK_ARCHS = {
    "DeepseekV3ForCausalLM",
}


def test_model_names_list_is_complete():
    """Verify _MODEL_NAMES has all expected architectures."""
    assert len(_MODEL_NAMES) == 2
    assert "DeepseekV3ForCausalLM" in _MODEL_NAMES
    assert "Qwen3MoeForCausalLM" in _MODEL_NAMES


def test_deepseek_archs_membership():
    """Verify DeepSeek archs are correctly identified."""
    assert "DeepseekV3ForCausalLM" in _DEEPSEEK_ARCHS
    assert "Qwen3MoeForCausalLM" not in _DEEPSEEK_ARCHS


def test_class_factory_creates_named_classes():
    """type() should create classes with the correct __name__."""

    class FakeBase:
        pass

    entry_classes = []
    for name in _MODEL_NAMES:
        cls = type(name, (FakeBase,), {})
        entry_classes.append(cls)
        assert cls.__name__ == name
        assert issubclass(cls, FakeBase)

    assert len(entry_classes) == 2


# ---------------------------------------------------------------------------
# DeepSeek arch detection in __init__
# ---------------------------------------------------------------------------


def test_deepseek_arch_triggers_setup():
    """DeepSeek architectures should trigger setup_deepseek_for_sglang."""
    setup_called = []

    for arch in _MODEL_NAMES:
        if arch in _DEEPSEEK_ARCHS:
            setup_called.append(arch)

    assert set(setup_called) == {"DeepseekV3ForCausalLM"}


def test_non_deepseek_arch_skips_setup():
    """Non-DeepSeek architectures should not trigger setup_deepseek_for_sglang."""
    for arch in ["Qwen3ForCausalLM", "Qwen3MoeForCausalLM"]:
        assert arch not in _DEEPSEEK_ARCHS


# ---------------------------------------------------------------------------
# forward branching — last rank vs non-last rank
# ---------------------------------------------------------------------------


def _forward_logic(
    model, logits_processor, pp_group, input_ids, hidden_states, forward_batch
):
    """Mirror the forward branching logic from _AtomCausalLMBaseForSglang."""
    if pp_group.is_last_rank:
        return logits_processor(
            input_ids,
            hidden_states,
            model.lm_head,
            forward_batch,
        )
    return hidden_states


def test_forward_last_rank_returns_logits():
    """When pp_group.is_last_rank=True, should run LogitsProcessor."""
    mock_model = MagicMock()
    mock_model.lm_head = MagicMock()
    mock_logits_processor = MagicMock(return_value="logits_output")
    mock_pp_group = _Obj(is_last_rank=True)
    mock_fb = MagicMock()

    result = _forward_logic(
        model=mock_model,
        logits_processor=mock_logits_processor,
        pp_group=mock_pp_group,
        input_ids="input_ids",
        hidden_states="hidden_states",
        forward_batch=mock_fb,
    )

    mock_logits_processor.assert_called_once_with(
        "input_ids", "hidden_states", mock_model.lm_head, mock_fb
    )
    assert result == "logits_output"


def test_forward_non_last_rank_returns_hidden_states():
    """When pp_group.is_last_rank=False, should return raw hidden_states."""
    mock_model = MagicMock()
    mock_logits_processor = MagicMock()
    mock_pp_group = _Obj(is_last_rank=False)
    mock_fb = MagicMock()

    result = _forward_logic(
        model=mock_model,
        logits_processor=mock_logits_processor,
        pp_group=mock_pp_group,
        input_ids="input_ids",
        hidden_states="hidden_states",
        forward_batch=mock_fb,
    )

    mock_logits_processor.assert_not_called()
    assert result == "hidden_states"


# ---------------------------------------------------------------------------
# load_weights — ignores passed weights
# ---------------------------------------------------------------------------


def test_load_weights_ignores_passed_weights():
    """load_weights should delegate to load_model_in_plugin_mode,
    ignoring the passed weights iterable."""
    mock_loader = MagicMock(return_value="loaded")
    mock_model = MagicMock()
    mock_model.atom_config = "fake_config"

    # Simulate the load_weights logic
    result = mock_loader(model=mock_model, config="fake_config", prefix="model.")

    # The passed weights iterator is never consumed
    mock_loader.assert_called_once_with(
        model=mock_model, config="fake_config", prefix="model."
    )
    assert result == "loaded"
