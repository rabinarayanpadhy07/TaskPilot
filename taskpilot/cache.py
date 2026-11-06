import logging
from django.core.cache.backends.db import DatabaseCache
from django.db import DatabaseError

logger = logging.getLogger(__name__)


class SafeDatabaseCache(DatabaseCache):
    """
    A DatabaseCache that automatically creates the cache table if missing,
    or safely fails open (returns default / False) rather than crashing DRF
    throttles or views with 500 errors if the table hasn't been created yet.
    """

    _attempted_create = False

    def _ensure_table(self):
        if not SafeDatabaseCache._attempted_create:
            SafeDatabaseCache._attempted_create = True
            try:
                from django.core.management import call_command

                call_command("createcachetable", self._table, verbosity=0)
                logger.info("Automatically created cache table: %s", self._table)
                return True
            except Exception as exc:
                logger.warning("Failed to auto-create cache table %s: %s", self._table, exc)
                return False
        return False

    def get(self, key, default=None, version=None):
        try:
            return super().get(key, default, version)
        except DatabaseError:
            if self._ensure_table():
                try:
                    return super().get(key, default, version)
                except DatabaseError:
                    pass
            return default

    def set(self, key, value, timeout=None, version=None):
        try:
            return super().set(key, value, timeout, version)
        except DatabaseError:
            if self._ensure_table():
                try:
                    return super().set(key, value, timeout, version)
                except DatabaseError:
                    pass
            return False

    def add(self, key, value, timeout=None, version=None):
        try:
            return super().add(key, value, timeout, version)
        except DatabaseError:
            if self._ensure_table():
                try:
                    return super().add(key, value, timeout, version)
                except DatabaseError:
                    pass
            return False

    def delete(self, key, version=None):
        try:
            return super().delete(key, version)
        except DatabaseError:
            return False

    def touch(self, key, timeout=None, version=None):
        try:
            return super().touch(key, timeout, version)
        except DatabaseError:
            return False

    def clear(self):
        try:
            super().clear()
        except DatabaseError:
            pass
