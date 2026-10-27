import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

from initialize_disposable_database import main


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISPOSABLE_URL = "postgresql+psycopg://127.0.0.1:5432/spsfrms_disposable"


class StartupSafetyTests(unittest.TestCase):
    def test_importing_app_never_runs_startup_database_operations(self):
        import_check = r"""
import os
import dotenv
from unittest.mock import patch
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.engine import Engine

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "synthetic-startup-test-key-for-isolated-tests"
os.environ["SPSFRMS_STARTUP_MODE"] = "initialize"
with patch.object(dotenv, "load_dotenv", return_value=False), \
     patch.object(SQLAlchemy, "create_all", side_effect=AssertionError("create_all called")) as create_all, \
     patch.object(Engine, "connect", side_effect=AssertionError("connect called")) as connect, \
     patch.object(Engine, "begin", side_effect=AssertionError("begin called")) as begin:
    import app
    assert app.app.config.get("DATABASE_INITIALIZATION_STATE", "not_run") == "not_run"
    create_all.assert_not_called()
    connect.assert_not_called()
    begin.assert_not_called()
"""
        safe_environment = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP")
            if key in os.environ
        }
        safe_environment.update({
            "DATABASE_URL": "sqlite://",
            "SPSFRMS_STARTUP_MODE": "initialize",
            "PYTHONPATH": str(PROJECT_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        result = subprocess.run(
            [sys.executable, "-c", import_check],
            cwd=PROJECT_ROOT,
            env=safe_environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_fails_closed_when_secret_key_is_missing_or_weak(self):
        import_check = r"""
import dotenv
from unittest.mock import patch
try:
    with patch.object(dotenv, "load_dotenv", return_value=False):
        import app
except RuntimeError as error:
    assert str(error) == "SECRET_KEY must be explicitly configured with at least 32 non-whitespace characters."
else:
    raise AssertionError("app imported without a sufficiently strong SECRET_KEY")
"""
        for secret_value in (None, "weak-test-key"):
            safe_environment = {
                key: os.environ[key]
                for key in ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP")
                if key in os.environ
            }
            safe_environment.update({
                "DATABASE_URL": "sqlite://",
                "PYTHONPATH": str(PROJECT_ROOT),
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            if secret_value is not None:
                safe_environment["SECRET_KEY"] = secret_value
            result = subprocess.run(
                [sys.executable, "-c", import_check],
                cwd=PROJECT_ROOT,
                env=safe_environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_setup_requires_flag_before_running_migrations(self):
        migration_runner = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [],
                environ={"SPSFRMS_DISPOSABLE_DATABASE_URL": DISPOSABLE_URL},
                migration_main=migration_runner,
            )
        self.assertEqual(result, 2)
        migration_runner.assert_not_called()

    def test_setup_does_not_fall_back_to_database_url(self):
        migration_runner = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--confirm-disposable-database"],
                environ={"DATABASE_URL": "postgresql+psycopg://remote.invalid/project"},
                input_fn=lambda _prompt: "INITIALIZE",
                migration_main=migration_runner,
            )
        self.assertEqual(result, 2)
        migration_runner.assert_not_called()
        self.assertNotIn("remote.invalid", output.getvalue())

    def test_setup_requires_typed_confirmation_before_running_migrations(self):
        migration_runner = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--confirm-disposable-database"],
                environ={"SPSFRMS_DISPOSABLE_DATABASE_URL": DISPOSABLE_URL},
                input_fn=lambda _prompt: "CANCEL",
                migration_main=migration_runner,
            )
        self.assertEqual(result, 2)
        migration_runner.assert_not_called()

    def test_setup_rejects_non_loopback_database_without_running_migrations(self):
        migration_runner = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--confirm-disposable-database"],
                environ={
                    "SPSFRMS_DISPOSABLE_DATABASE_URL":
                        "postgresql+psycopg://example.invalid/spsfrms_disposable",
                },
                input_fn=lambda _prompt: "INITIALIZE",
                migration_main=migration_runner,
            )
        self.assertEqual(result, 2)
        migration_runner.assert_not_called()

    def test_setup_delegates_to_versioned_runner_after_confirmations(self):
        migration_runner = Mock(side_effect=lambda _argv, **kwargs: (
            0 if kwargs["input_fn"]("confirmation") ==
            "APPLY MIGRATIONS disposable-local spsfrms_disposable" else 2
        ))
        environment = {
            "SPSFRMS_DISPOSABLE_DATABASE_URL": DISPOSABLE_URL,
            "DATABASE_URL": "postgresql+psycopg://old.invalid/ignored",
        }
        answers = iter(("INITIALIZE", "APPLY MIGRATIONS disposable-local spsfrms_disposable"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--confirm-disposable-database"],
                environ=environment,
                input_fn=lambda _prompt: next(answers),
                migration_main=migration_runner,
            )
        self.assertEqual(result, 0)
        migration_runner.assert_called_once()
        self.assertEqual(migration_runner.call_args.args[0], [
            "--apply", "--target", "disposable-local",
            "--expected-database-name", "spsfrms_disposable",
        ])
        self.assertEqual(
            migration_runner.call_args.kwargs["environ"]["SPSFRMS_MIGRATION_DATABASE_URL"],
            DISPOSABLE_URL,
        )
        self.assertEqual(environment["DATABASE_URL"], "postgresql+psycopg://old.invalid/ignored")
        self.assertNotIn("old.invalid", output.getvalue())
        self.assertNotIn(DISPOSABLE_URL, output.getvalue())

    def test_setup_failure_is_reported_without_raw_exception_details(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--confirm-disposable-database"],
                environ={"SPSFRMS_DISPOSABLE_DATABASE_URL": DISPOSABLE_URL},
                input_fn=lambda _prompt: "INITIALIZE",
                migration_main=Mock(side_effect=RuntimeError("private connection detail")),
            )
        self.assertEqual(result, 1)
        self.assertNotIn("private connection detail", output.getvalue())


if __name__ == "__main__":
    unittest.main()
