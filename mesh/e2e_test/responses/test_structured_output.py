"""Structured output tests for Response API.

Tests for text.format field with json_schema format.

Source: Migrated from e2e_response_api/features/test_structured_output.py
"""

from __future__ import annotations

import json
import logging

import pytest

logger = logging.getLogger(__name__)


# =============================================================================
# Local Backend Tests (gRPC with Qwen model - simple schema)
# =============================================================================


@pytest.mark.e2e
@pytest.mark.model("qwen-14b")
@pytest.mark.gateway(
    extra_args=["--tool-call-parser", "qwen"]
)
@pytest.mark.parametrize("setup_backend", ["grpc"], indirect=True)
class TestSimpleSchemaStructuredOutput:
    """Structured output tests with simpler schema for models that don't
    handle complex schemas well.
    """

    def test_structured_output_json_schema(self, setup_backend):
        """Test structured output with simple json_schema format."""
        _, model, client, gateway = setup_backend

        params = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": "You are a math solver. Return ONLY a JSON object that matches the schema-no extra text.",
                },
                {
                    "role": "user",
                    "content": "What is 1 + 1?",
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "math_answer",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                    },
                }
            },
        }

        create_resp = client.responses.create(**params)
        assert create_resp.error is None
        assert create_resp.id is not None
        assert create_resp.output is not None
        assert create_resp.text is not None

        # Verify text format was echoed back correctly
        assert create_resp.text.format is not None
        assert create_resp.text.format.type == "json_schema"
        assert create_resp.text.format.name == "math_answer"
        assert create_resp.text.format.schema_ is not None

        # Find the message output
        output_text = next(
            (
                content.text
                for item in create_resp.output
                if item.type == "message"
                for content in item.content
                if content.type == "output_text"
            ),
            None,
        )

        assert output_text is not None, "No output_text found in response"
        assert output_text.strip(), "output_text is empty"

        # Parse JSON output
        output_json = json.loads(output_text)

        # Verify simple schema structure (just answer field)
        assert "answer" in output_json
        assert isinstance(output_json["answer"], str)
        assert output_json["answer"], "Answer is empty"
