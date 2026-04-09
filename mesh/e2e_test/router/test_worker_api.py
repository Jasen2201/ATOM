"""Tests for gateway worker management APIs.

Tests the gateway's worker management endpoints:
- GET /workers - List all workers
- GET /v1/models - List available models

Usage:
    pytest e2e_test/router/test_worker_api.py -v
"""

from __future__ import annotations

import logging

import pytest

logger = logging.getLogger(__name__)


@pytest.mark.e2e
@pytest.mark.parametrize("setup_backend", ["grpc", "http"], indirect=True)
class TestWorkerAPI:
    """Tests for worker management APIs using setup_backend fixture."""

    def test_list_workers(self, setup_backend):
        """Test listing workers via /workers endpoint."""
        backend, model, client, gateway = setup_backend

        workers = gateway.list_workers()
        assert len(workers) >= 1, "Expected at least one worker"
        logger.info("Found %d workers", len(workers))

        for worker in workers:
            logger.info(
                "Worker: id=%s, url=%s, status=%s",
                worker.id,
                worker.url,
                worker.status,
            )
            assert worker.url, "Worker should have a URL"

    def test_list_models(self, setup_backend):
        """Test listing models via /v1/models endpoint."""
        backend, model, client, gateway = setup_backend

        models = gateway.list_models()
        assert len(models) >= 1, "Expected at least one model"
        logger.info("Found %d models", len(models))

        for m in models:
            logger.info("Model: %s", m.get("id", "unknown"))
            assert "id" in m, "Model should have an id"

    def test_health_endpoint(self, setup_backend):
        """Test health check endpoint."""
        backend, model, client, gateway = setup_backend

        assert gateway.health(), "Gateway should be healthy"
        logger.info("Gateway health check passed")
