import argparse
import os
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from core.base.config_models import (
    DatabaseSettings,
    PandaGalleryConfig,
    generate_default_yaml_content,
    get_config_schema,
)
from core.base.setup import Settings
from pandabackup.settings import configure_database, get_database_config


class DatabaseSettingsConfigModelTestCase(SimpleTestCase):
    """Test DatabaseSettings model defaults, validation, and schema generation."""

    def test_default_values(self):
        db_settings = DatabaseSettings()
        self.assertEqual(db_settings.postgresql_name, "pandaweb")
        self.assertEqual(db_settings.postgresql_user, "pandaadmin")
        self.assertEqual(db_settings.postgresql_password, "pandaadmin")
        self.assertEqual(db_settings.postgresql_host, "localhost")
        self.assertEqual(db_settings.postgresql_port, "5432")
        self.assertEqual(db_settings.conn_max_age, 600)
        self.assertIsNone(db_settings.worker_conn_max_age)
        self.assertTrue(db_settings.conn_health_checks)
        self.assertFalse(db_settings.pooling)
        self.assertEqual(db_settings.pool_min_size, 4)
        self.assertEqual(db_settings.pool_max_size, 10)
        self.assertEqual(db_settings.pool_timeout, 30.0)
        self.assertIsNone(db_settings.pool_max_lifetime)
        self.assertEqual(db_settings.pool_max_idle, 600.0)

    def test_coercion_and_validators(self):
        # Strings to numeric / none
        db_settings = DatabaseSettings(
            postgresql_port=5433,
            conn_max_age="300",
            worker_conn_max_age="none",
            pooling=True,
            pool_min_size="2",
            pool_max_size="null",
            pool_timeout="45.5",
            pool_max_lifetime="",
            pool_max_idle="120.0",
        )
        self.assertEqual(db_settings.postgresql_port, "5433")
        self.assertEqual(db_settings.conn_max_age, 300)
        self.assertIsNone(db_settings.worker_conn_max_age)
        self.assertTrue(db_settings.pooling)
        self.assertEqual(db_settings.pool_min_size, 2)
        self.assertIsNone(db_settings.pool_max_size)
        self.assertEqual(db_settings.pool_timeout, 45.5)
        self.assertIsNone(db_settings.pool_max_lifetime)
        self.assertEqual(db_settings.pool_max_idle, 120.0)

    def test_schema_and_default_yaml_generation(self):
        import yaml

        schema = get_config_schema()
        db_props = schema["$defs"]["DatabaseSettings"]["properties"]
        self.assertIn("conn_max_age", db_props)
        self.assertIn("worker_conn_max_age", db_props)
        self.assertIn("conn_health_checks", db_props)
        self.assertIn("pooling", db_props)
        self.assertIn("pool_min_size", db_props)
        self.assertIn("pool_max_size", db_props)
        self.assertIn("pool_timeout", db_props)

        yaml_content = generate_default_yaml_content()
        parsed = yaml.safe_load(yaml_content)
        validated = PandaGalleryConfig.model_validate(parsed)
        self.assertEqual(validated.database.conn_max_age, 600)
        self.assertTrue(validated.database.conn_health_checks)
        self.assertFalse(validated.database.pooling)


class EnvironmentVariableOverridesTestCase(SimpleTestCase):
    """Test loading database connection and pooling parameters from environment variables."""

    def test_load_pooling_and_conn_age_from_environment(self):
        env_vars = {
            "DATABASE_CONN_MAX_AGE": "180",
            "DATABASE_WORKER_CONN_MAX_AGE": "60",
            "DATABASE_CONN_HEALTH_CHECKS": "false",
            "DATABASE_POOLING_ENABLE": "true",
            "DATABASE_POOL_MIN_SIZE": "5",
            "DATABASE_POOL_MAX_SIZE": "15",
            "DATABASE_POOL_TIMEOUT": "20.5",
            "DATABASE_POOL_MAX_LIFETIME": "3600.0",
            "DATABASE_POOL_MAX_IDLE": "300.0",
        }
        with patch.dict(os.environ, env_vars):
            settings = Settings(load_from_disk=False)
            settings.load_from_environment()
            self.assertEqual(settings.database["conn_max_age"], 180)
            self.assertEqual(settings.database["worker_conn_max_age"], 60)
            self.assertFalse(settings.database["conn_health_checks"])
            self.assertTrue(settings.database["pooling"])
            self.assertEqual(settings.database["pool_min_size"], 5)
            self.assertEqual(settings.database["pool_max_size"], 15)
            self.assertEqual(settings.database["pool_timeout"], 20.5)
            self.assertEqual(settings.database["pool_max_lifetime"], 3600.0)
            self.assertEqual(settings.database["pool_max_idle"], 300.0)

    def test_load_null_conn_max_age_from_environment(self):
        with patch.dict(os.environ, {"DATABASE_CONN_MAX_AGE": "null", "DATABASE_WORKER_CONN_MAX_AGE": "none"}):
            settings = Settings(load_from_disk=False)
            settings.load_from_environment()
            self.assertIsNone(settings.database["conn_max_age"])
            self.assertIsNone(settings.database["worker_conn_max_age"])


class GetDatabaseConfigTestCase(SimpleTestCase):
    """Test get_database_config and configure_database behavior for web and workers."""

    def test_web_server_default_config(self):
        settings = Settings(load_from_disk=False)
        settings.is_worker_process = False
        config = get_database_config(settings)["default"]

        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(config["CONN_MAX_AGE"], 600)
        self.assertTrue(config["CONN_HEALTH_CHECKS"])
        self.assertEqual(config["OPTIONS"], {})

    def test_worker_process_uses_worker_conn_max_age_if_set(self):
        settings = Settings(load_from_disk=False)
        settings.is_worker_process = True
        settings.database["conn_max_age"] = 600
        settings.database["worker_conn_max_age"] = 120

        config = get_database_config(settings)["default"]
        self.assertEqual(config["CONN_MAX_AGE"], 120)

    def test_worker_process_falls_back_to_conn_max_age_if_worker_override_is_none(self):
        settings = Settings(load_from_disk=False)
        settings.is_worker_process = True
        settings.database["conn_max_age"] = 600
        settings.database["worker_conn_max_age"] = None

        config = get_database_config(settings)["default"]
        self.assertEqual(config["CONN_MAX_AGE"], 600)

    def test_pooling_forces_conn_max_age_to_zero(self):
        settings = Settings(load_from_disk=False)
        settings.database["pooling"] = True
        settings.database["conn_max_age"] = 600
        settings.database["pool_min_size"] = 2
        settings.database["pool_max_size"] = 8
        settings.database["pool_timeout"] = 15.0
        settings.database["pool_max_idle"] = 180.0

        config = get_database_config(settings)["default"]
        self.assertEqual(config["CONN_MAX_AGE"], 0)
        self.assertIn("pool", config["OPTIONS"])
        pool_opts = config["OPTIONS"]["pool"]
        self.assertEqual(pool_opts["min_size"], 2)
        self.assertEqual(pool_opts["max_size"], 8)
        self.assertEqual(pool_opts["timeout"], 15.0)
        self.assertEqual(pool_opts["max_idle"], 180.0)

    def test_unsupported_engine_raises_value_error(self):
        settings = Settings(load_from_disk=False)
        settings.db_engine = "sqlite3"
        with self.assertRaises(ValueError) as ctx:
            get_database_config(settings)
        self.assertIn("PostgreSQL is the only supported database backend", str(ctx.exception))

    def test_configure_database_updates_runtime(self):
        settings = Settings(load_from_disk=False)
        settings.database["conn_max_age"] = 450
        configure_database(settings)
        from pandabackup import settings as django_settings

        self.assertEqual(django_settings.DATABASES["default"]["CONN_MAX_AGE"], 450)


class ServerAndWorkerCleanupTestCase(TestCase):
    """Test connection pool and database connection cleanup hooks."""

    def test_run_gunicorn_registers_post_fork_and_worker_exit(self):
        from server import run_gunicorn

        args = argparse.Namespace(
            port=9999,
            socket_file=None,
            socket_queue_size=None,
            workers=2,
            threads=1,
            timeout=30,
            pidfile=None,
            daemonize=False,
        )
        settings = Settings(load_from_disk=False)
        with (
            patch("server.GunicornApplication") as mock_gapp,
            patch("pandabackup.settings.WORKERS.start_workers"),
            patch("django.db.connections.close_all") as mock_close_all,
            patch.object(Settings, "create_missing_directories"),
        ):
            run_gunicorn(settings, args)
            call_options = mock_gapp.call_args[0][1]
            self.assertIn("post_fork", call_options)
            self.assertIn("worker_exit", call_options)
            self.assertTrue(callable(call_options["post_fork"]))
            self.assertTrue(callable(call_options["worker_exit"]))

            # Test callbacks invoke connection cleanup
            mock_close_all.reset_mock()
            call_options["post_fork"](None, None)
            mock_close_all.assert_called_once()

            mock_close_all.reset_mock()
            call_options["worker_exit"](None, None)
            mock_close_all.assert_called_once()

    def test_worker_runner_safe_close_old_connections(self):
        from workers.runner import _safe_close_old_connections

        with patch("django.db.connection.in_atomic_block", False), patch("django.db.close_old_connections") as mock_close:
            _safe_close_old_connections()
            mock_close.assert_called_once()

        with patch("django.db.connection.in_atomic_block", True), patch("django.db.close_old_connections") as mock_close:
            _safe_close_old_connections()
            mock_close.assert_not_called()

        # Exception in close_old_connections does not propagate
        with patch("django.db.connection.in_atomic_block", False), patch(
            "django.db.close_old_connections", side_effect=RuntimeError("connection error")
        ):
            _safe_close_old_connections()
