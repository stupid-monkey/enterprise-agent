"""Connection recovery after a PostgreSQL restart must clear stale sockets."""

import unittest
import threading
from unittest.mock import Mock, patch

import psycopg

from app.api.postgres_memory_service import PostgresMemoryService


class PostgresMemoryServiceHealthTests(unittest.TestCase):
    def test_stale_connection_is_closed_and_reopened(self):
        service = object.__new__(PostgresMemoryService)
        stale = Mock()
        stale.connection.execute.side_effect = psycopg.OperationalError("connection lost")
        fresh = Mock()
        fresh.connection.execute.return_value = None
        service._repository = Mock(side_effect=(stale, fresh))
        service.conversations = Mock()
        self.assertTrue(service.healthy())
        stale.close.assert_called_once()
        fresh.connection.execute.assert_called_once_with("SELECT 1")
        service.conversations.delete_outbox_metrics.assert_called_once()

    def test_thread_local_closed_connection_can_be_replaced_even_if_untracked(self):
        service = object.__new__(PostgresMemoryService)
        service._local = threading.local()
        stale = Mock()
        stale.connection.closed = True
        service._local.repository = stale
        service._lock = threading.Lock()
        service._repositories = []
        service._closed = False
        service.dsn = "postgresql://unused"
        service.schema = "memory"
        service.default_auto_suggest = True
        fresh = Mock()
        with patch("app.api.postgres_memory_service.PgUserMemoryRepository",
                   return_value=fresh):
            self.assertIs(service._repository(), fresh)
        stale.close.assert_called_once()
        self.assertEqual(service._repositories, [fresh])
