import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
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

    def test_setup_requires_flag_before_importing_application(self):
        app_loader = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [],
                environ={"SPSFRMS_DISPOSABLE_DATABASE_URL": DISPOSABLE_URL},
                app_loader=app_loader,
            )
        self.assertEqual(result, 2)
        app_loader.assert_not_called()

    def test_setup_does_not_fall_back_to_database_url(self):
        app_loader = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--confirm-disposable-database"],
                environ={"DATABASE_URL": "postgresql+psycopg://remote.invalid/project"},
                input_fn=lambda _prompt: "INITIALIZE",
                app_loader=app_loader,
            )
        self.assertEqual(result, 2)
        app_loader.assert_not_called()
        self.assertNotIn("remote.invalid", output.getvalue())

    def test_setup_requires_typed_confirmation_before_importing_application(self):
        app_loader = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--confirm-disposable-database"],
                environ={"SPSFRMS_DISPOSABLE_DATABASE_URL": DISPOSABLE_URL},
                input_fn=lambda _prompt: "CANCEL",
                app_loader=app_loader,
            )
        self.assertEqual(result, 2)
        app_loader.assert_not_called()

    def test_setup_rejects_non_loopback_database_without_importing_application(self):
        app_loader = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--confirm-disposable-database"],
                environ={
                    "SPSFRMS_DISPOSABLE_DATABASE_URL":
                        "postgresql+psycopg://example.invalid/spsfrms_disposable",
                },
                input_fn=lambda _prompt: "INITIALIZE",
                app_loader=app_loader,
            )
        self.assertEqual(result, 2)
        app_loader.assert_not_called()

    def test_setup_calls_only_initializer_after_both_confirmations(self):
        initialize = Mock()
        fake_app = SimpleNamespace(
            initialize_database=initialize,
            db=SimpleNamespace(session=SimpleNamespace(rollback=Mock())),
        )
        app_loader = Mock(return_value=fake_app)
        environment = {
            "SPSFRMS_DISPOSABLE_DATABASE_URL": DISPOSABLE_URL,
            "DATABASE_URL": "postgresql+psycopg://old.invalid/ignored",
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--confirm-disposable-database"],
                environ=environment,
                input_fn=lambda _prompt: "INITIALIZE",
                app_loader=app_loader,
            )
        self.assertEqual(result, 0)
        self.assertEqual(environment["DATABASE_URL"], DISPOSABLE_URL)
        app_loader.assert_called_once_with()
        initialize.assert_called_once_with()
        self.assertNotIn("old.invalid", output.getvalue())
        self.assertNotIn(DISPOSABLE_URL, output.getvalue())

    def test_setup_failure_is_reported_without_raw_exception_details(self):
        rollback = Mock()
        fake_app = SimpleNamespace(
            initialize_database=Mock(side_effect=RuntimeError("private connection detail")),
            db=SimpleNamespace(session=SimpleNamespace(rollback=rollback)),
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                ["--confirm-disposable-database"],
                environ={"SPSFRMS_DISPOSABLE_DATABASE_URL": DISPOSABLE_URL},
                input_fn=lambda _prompt: "INITIALIZE",
                app_loader=lambda: fake_app,
            )
        self.assertEqual(result, 1)
        rollback.assert_called_once_with()
        self.assertNotIn("private connection detail", output.getvalue())


if __name__ == "__main__":
    unittest.main()
