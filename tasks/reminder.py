"""
Backwards-compatible re-exports.

The reminder persistence implementation moved to storage/repositories.py
(Postgres/asyncpg); these aliases keep existing imports working.
"""

from storage.repositories import ReminderRepository as ReminderManager
from storage.repositories import reminder_repo as reminder_manager

__all__ = ["ReminderManager", "reminder_manager"]
