"""Smoke tests for the relocated periodic Celery tasks.

Production-criticality:

- ``cleanup_pending_tool_state`` replaces Mongo's TTL index for the
  ``pending_tool_state`` table. Missing schedule = unbounded growth.
- ``purge_old_user_data`` enforces the 30-day privacy retention
  (within the Aztec Foundation 12-month maximum; matches the Discord
  Message Content intent commitments). Missing schedule = compliance miss.

The previous ``@celery.on_after_configure.connect`` decorator did NOT
fire when this module was loaded via ``celeryconfig.py:imports`` —
``configure`` had already fired by then. We switched to
``on_after_finalize.connect`` so the schedule registers at worker
boot. These tests guard against future regressions of that signal
choice and against the schedule entries being silently dropped.
"""

from unittest.mock import MagicMock

import pytest

from application.workers import periodic


@pytest.mark.unit
class TestPeriodicTaskRegistration:
    def test_setup_periodic_tasks_registers_both_schedules(self):
        """Calling the signal handler should add two beat schedules."""
        sender = MagicMock()
        periodic.setup_periodic_tasks(sender)

        # Two add_periodic_task calls, one per schedule.
        assert sender.add_periodic_task.call_count == 2

        names = {
            call.kwargs.get("name")
            for call in sender.add_periodic_task.call_args_list
        }
        assert "cleanup-pending-tool-state" in names
        assert "purge-old-user-data" in names

    def test_signal_uses_on_after_finalize(self):
        """Regression: setup_periodic_tasks must be wired to
        ``on_after_finalize``, NOT ``on_after_configure``. ``configure``
        fires before ``celeryconfig.imports`` loads this module, so the
        upstream signal choice silently registered no schedules.

        We verify via source inspection rather than by introspecting
        the live signal — by the time tests run, the signal has
        already fired and receivers may have been cleared.
        """
        import inspect

        src = inspect.getsource(periodic)
        assert "on_after_finalize.connect" in src, (
            "setup_periodic_tasks must use @celery.on_after_finalize.connect; "
            "@celery.on_after_configure.connect fires before celeryconfig "
            "loads this module via 'imports' and silently drops the schedule."
        )
        assert "on_after_configure.connect" not in src, (
            "Regression: on_after_configure was the broken choice — see the "
            "comment in workers/periodic.py."
        )


@pytest.mark.unit
class TestPurgeOldUserDataNoOps:
    def test_skipped_when_no_postgres_uri(self, monkeypatch):
        """When POSTGRES_URI is unset the task must short-circuit instead
        of crashing — matters for ad-hoc local environments.
        """
        from application.core import settings as settings_module

        monkeypatch.setattr(settings_module.settings, "POSTGRES_URI", None)
        result = periodic.purge_old_user_data.run(retention_days=30)
        assert result == {"skipped": "POSTGRES_URI not set"}


@pytest.mark.unit
class TestCleanupPendingToolStateNoOps:
    def test_skipped_when_no_postgres_uri(self, monkeypatch):
        from application.core import settings as settings_module

        monkeypatch.setattr(settings_module.settings, "POSTGRES_URI", None)
        result = periodic.cleanup_pending_tool_state.run()
        assert result == {"deleted": 0, "skipped": "POSTGRES_URI not set"}
