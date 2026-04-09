"""State management tests for Response API.

Tests previous_response_id-based state management against local gRPC backend.

Source: Migrated from e2e_response_api/features/test_state_management.py
"""

from __future__ import annotations

import logging

import openai
import pytest

logger = logging.getLogger(__name__)


# =============================================================================
# Local Backend Tests (gRPC with Qwen model)
# =============================================================================


@pytest.mark.e2e
@pytest.mark.model("qwen-14b")
@pytest.mark.gateway(
    extra_args=["--tool-call-parser", "qwen"]
)
@pytest.mark.parametrize("setup_backend", ["grpc"], indirect=True)
class TestStateManagementLocal:
    """State management tests against local gRPC backend."""

    @pytest.mark.skip(reason="TODO: Add the invalid previous_response_id check")
    def test_previous_response_id_invalid(self, setup_backend):
        """Test using invalid previous_response_id."""
        _, model, client, gateway = setup_backend
        with pytest.raises(openai.BadRequestError):
            client.responses.create(
                model=model,
                input="Test",
                previous_response_id="resp_invalid123",
                max_output_tokens=50,
            )

    def test_basic_response_creation(self, setup_backend):
        """Test basic response creation without state."""
        _, model, client, gateway = setup_backend

        resp = client.responses.create(model=model, input="What is 2+2?")

        assert resp.id is not None
        assert resp.error is None
        assert resp.status == "completed"
        assert len(resp.output_text) > 0
        assert resp.usage is not None

    def test_streaming_response(self, setup_backend):
        """Test streaming response."""
        _, model, client, gateway = setup_backend

        resp = client.responses.create(
            model=model, input="Count to 5", stream=True, max_output_tokens=50
        )

        events = list(resp)
        created_events = [e for e in events if e.type == "response.created"]
        assert len(created_events) > 0

        assert any(
            e.type in ["response.completed", "response.in_progress"] for e in events
        )

    def test_previous_response_id_chaining(self, setup_backend):
        """Test chaining responses using previous_response_id."""
        _, model, client, gateway = setup_backend

        # First response
        resp1 = client.responses.create(
            model=model, input="My name is Alice and my friend is Bob. Remember it."
        )
        assert resp1.error is None
        assert resp1.status == "completed"

        # Second response referencing first
        resp2 = client.responses.create(
            model=model, input="What is my name", previous_response_id=resp1.id
        )
        assert resp2.error is None
        assert resp2.status == "completed"
        assert "Alice" in resp2.output_text

        # Third response referencing second
        resp3 = client.responses.create(
            model=model,
            input="What is my friend name?",
            previous_response_id=resp2.id,
        )
        assert resp3.error is None
        assert resp3.status == "completed"
        assert "Bob" in resp3.output_text

    def test_mutually_exclusive_parameters(self, setup_backend):
        """Test that previous_response_id and conversation are mutually exclusive."""
        _, model, client, gateway = setup_backend

        conversation_id = "conv_123"
        resp1 = client.responses.create(model=model, input="Test")

        with pytest.raises(openai.BadRequestError):
            client.responses.create(
                model=model,
                input="This should fail",
                previous_response_id=resp1.id,
                conversation=conversation_id,
            )
