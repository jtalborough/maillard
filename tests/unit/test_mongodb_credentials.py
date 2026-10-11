"""Regression tests for MongoDB credential handling."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from maillard.core.config import MaillardConfig, _LegacyMongoEndpoint

_SENTINEL_USERNAME = "maillard-app-user"
_SENTINEL_PASSWORD = "test-only-password-marker"


def _credential_files(tmp_path: Path) -> tuple[Path, Path]:
    username_file = tmp_path / "mongodb-username"
    password_file = tmp_path / "mongodb-password"
    username_file.write_text(f"{_SENTINEL_USERNAME}\n", encoding="utf-8")
    password_file.write_text(f"{_SENTINEL_PASSWORD}\n", encoding="utf-8")
    username_file.chmod(0o400)
    password_file.chmod(0o400)
    return username_file, password_file


def _assert_validation_error_is_secret_safe(
    exc: ValidationError, *sentinels: str
) -> None:
    rendered = (str(exc), str(exc.errors()), exc.json())
    for sentinel in sentinels:
        assert all(sentinel not in output for output in rendered)


class TestMongoCredentialConfig:
    def test_reads_bounded_credentials_from_files_without_serializing_values(
        self, tmp_path: Path
    ) -> None:
        username_file, password_file = _credential_files(tmp_path)
        config = MaillardConfig(
            _env_file=None,
            mongodb_host="mongo.apps.svc.cluster.local",
            mongodb_port=27018,
            mongodb_name="maillard",
            mongodb_auth_source="maillard",
            mongodb_username_file=username_file,
            mongodb_password_file=password_file,
        )

        assert config.load_mongodb_credentials() == (
            _SENTINEL_USERNAME,
            _SENTINEL_PASSWORD,
        )
        serialized = f"{config!r} {config.model_dump()} {config.model_dump_json()}"
        assert _SENTINEL_USERNAME not in serialized
        assert _SENTINEL_PASSWORD not in serialized

    def test_requires_both_credential_files(self, tmp_path: Path) -> None:
        username_file, _ = _credential_files(tmp_path)

        with pytest.raises(ValidationError, match="configured together"):
            MaillardConfig(
                _env_file=None,
                mongodb_username_file=username_file,
            )

    def test_rejects_oversized_secret_without_echoing_it(self, tmp_path: Path) -> None:
        username_file, password_file = _credential_files(tmp_path)
        oversized_marker = "oversized-secret-marker-" + ("x" * 5000)
        password_file.chmod(0o600)
        password_file.write_text(oversized_marker, encoding="utf-8")
        password_file.chmod(0o400)
        config = MaillardConfig(
            _env_file=None,
            mongodb_username_file=username_file,
            mongodb_password_file=password_file,
        )

        with pytest.raises(ValueError) as exc_info:
            config.load_mongodb_credentials()

        assert "too large" in str(exc_info.value)
        assert oversized_marker not in str(exc_info.value)

    @pytest.mark.parametrize("mode", [0o644, 0o666])
    def test_rejects_overpermissive_credential_file(
        self, tmp_path: Path, mode: int
    ) -> None:
        username_file, password_file = _credential_files(tmp_path)
        password_file.chmod(mode)
        config = MaillardConfig(
            _env_file=None,
            mongodb_username_file=username_file,
            mongodb_password_file=password_file,
        )

        with pytest.raises(ValueError, match="permissions are not secure"):
            config.load_mongodb_credentials()

    @pytest.mark.parametrize("mode", [0o400, 0o440])
    def test_accepts_secure_credential_file_modes(self, tmp_path: Path, mode: int) -> None:
        username_file, password_file = _credential_files(tmp_path)
        username_file.chmod(mode)
        password_file.chmod(mode)
        config = MaillardConfig(
            _env_file=None,
            mongodb_username_file=username_file,
            mongodb_password_file=password_file,
        )

        assert config.load_mongodb_credentials() == (
            _SENTINEL_USERNAME,
            _SENTINEL_PASSWORD,
        )

    def test_rejects_fifo_without_blocking(self, tmp_path: Path) -> None:
        username_file, _ = _credential_files(tmp_path)
        fifo = tmp_path / "mongodb-password-fifo"
        os.mkfifo(fifo, 0o400)
        config = MaillardConfig(
            _env_file=None,
            mongodb_username_file=username_file,
            mongodb_password_file=fifo,
        )

        with pytest.raises(ValueError, match="must be a regular file"):
            config.load_mongodb_credentials()

    @pytest.mark.parametrize("non_file", ["directory", "device"])
    def test_rejects_non_regular_credential_target(
        self, tmp_path: Path, non_file: str
    ) -> None:
        username_file, _ = _credential_files(tmp_path)
        if non_file == "directory":
            password_target = tmp_path / "password-directory"
            password_target.mkdir(mode=0o700)
        else:
            password_target = Path("/dev/null")
            if not password_target.exists():
                pytest.skip("portable character device is unavailable")
        config = MaillardConfig(
            _env_file=None,
            mongodb_username_file=username_file,
            mongodb_password_file=password_target,
        )

        with pytest.raises(ValueError, match="must be a regular file"):
            config.load_mongodb_credentials()

    def test_rejects_untrusted_credential_file_owner(self, tmp_path: Path) -> None:
        import maillard.core.config as config_module

        username_file, password_file = _credential_files(tmp_path)
        config = MaillardConfig(
            _env_file=None,
            mongodb_username_file=username_file,
            mongodb_password_file=password_file,
        )
        real_fstat = os.fstat

        def untrusted_fstat(fd: int) -> os.stat_result:
            fields = list(real_fstat(fd))
            fields[4] = max(os.geteuid(), 0) + 1000
            return os.stat_result(fields)

        with (
            patch.object(config_module.os, "fstat", side_effect=untrusted_fstat),
            pytest.raises(ValueError, match="owner is not trusted"),
        ):
            config.load_mongodb_credentials()

    def test_accepts_kubernetes_projected_symlinks_and_rotation(self, tmp_path: Path) -> None:
        first = tmp_path / "..data-v1"
        second = tmp_path / "..data-v2"
        first.mkdir()
        second.mkdir()
        for directory, suffix in ((first, "v1"), (second, "v2")):
            (directory / "mongodb-username").write_text(
                f"{_SENTINEL_USERNAME}-{suffix}", encoding="utf-8"
            )
            (directory / "mongodb-password").write_text(
                f"{_SENTINEL_PASSWORD}-{suffix}", encoding="utf-8"
            )
            (directory / "mongodb-username").chmod(0o440)
            (directory / "mongodb-password").chmod(0o440)

        (tmp_path / "..data").symlink_to(first.name, target_is_directory=True)
        username_file = tmp_path / "mongodb-username"
        password_file = tmp_path / "mongodb-password"
        username_file.symlink_to("..data/mongodb-username")
        password_file.symlink_to("..data/mongodb-password")
        config = MaillardConfig(
            _env_file=None,
            mongodb_username_file=username_file,
            mongodb_password_file=password_file,
        )

        assert config.load_mongodb_credentials() == (
            f"{_SENTINEL_USERNAME}-v1",
            f"{_SENTINEL_PASSWORD}-v1",
        )

        next_link = tmp_path / "..data-next"
        next_link.symlink_to(second.name, target_is_directory=True)
        os.replace(next_link, tmp_path / "..data")
        assert config.load_mongodb_credentials() == (
            f"{_SENTINEL_USERNAME}-v2",
            f"{_SENTINEL_PASSWORD}-v2",
        )

    def test_chunked_reads_never_exceed_total_bound(self, tmp_path: Path) -> None:
        import maillard.core.config as config_module

        _, password_file = _credential_files(tmp_path)
        password_file.chmod(0o600)
        password_file.write_bytes(b"x" * 5000)
        password_file.chmod(0o400)
        real_read = os.read
        bytes_returned = 0

        def chunked_read(fd: int, count: int) -> bytes:
            nonlocal bytes_returned
            chunk = real_read(fd, min(count, 257))
            bytes_returned += len(chunk)
            return chunk

        with (
            patch.object(config_module.os, "read", side_effect=chunked_read),
            pytest.raises(ValueError, match="too large"),
        ):
            MaillardConfig._read_credential_file(password_file, "password")

        assert bytes_returned == 4097

    def test_credential_descriptor_closes_after_success(self, tmp_path: Path) -> None:
        import maillard.core.config as config_module

        _, password_file = _credential_files(tmp_path)
        real_close = os.close
        with patch.object(config_module.os, "close", wraps=real_close) as close:
            value = MaillardConfig._read_credential_file(password_file, "password")

        assert value == _SENTINEL_PASSWORD
        close.assert_called_once()

    def test_credential_descriptor_closes_after_permission_rejection(
        self, tmp_path: Path
    ) -> None:
        import maillard.core.config as config_module

        _, password_file = _credential_files(tmp_path)
        password_file.chmod(0o644)
        real_close = os.close
        with (
            patch.object(config_module.os, "close", wraps=real_close) as close,
            pytest.raises(ValueError, match="permissions are not secure"),
        ):
            MaillardConfig._read_credential_file(password_file, "password")

        close.assert_called_once()

    def test_credential_descriptor_closes_after_read_error(self, tmp_path: Path) -> None:
        import maillard.core.config as config_module

        _, password_file = _credential_files(tmp_path)
        real_close = os.close
        with (
            patch.object(config_module.os, "read", side_effect=OSError),
            patch.object(config_module.os, "close", wraps=real_close) as close,
            pytest.raises(ValueError, match="could not be read"),
        ):
            MaillardConfig._read_credential_file(password_file, "password")

        close.assert_called_once()

    def test_accepts_legacy_non_secret_uri(self) -> None:
        config = MaillardConfig(
            _env_file=None,
            mongodb_uri="mongodb://legacy-mongo.internal:27019",
        )

        assert config.mongodb_host == "legacy-mongo.internal"
        assert config.mongodb_port == 27019
        assert "mongodb_uri" not in config.model_dump()

    def test_legacy_uri_without_port_uses_default(self) -> None:
        config = MaillardConfig(_env_file=None, mongodb_uri="mongodb://mongo.internal")

        assert config.mongodb_port == 27017

    def test_legacy_uri_rejects_zero_port(self) -> None:
        with pytest.raises(ValidationError, match="invalid or unsupported"):
            MaillardConfig(_env_file=None, mongodb_uri="mongodb://mongo.internal:0")

    @pytest.mark.parametrize(
        "endpoint",
        [
            _LegacyMongoEndpoint(host="unsafe host", port=27017),
            _LegacyMongoEndpoint(host="mongo.internal", port=0),
            _LegacyMongoEndpoint(host="mongo.internal", port=65536),
        ],
    )
    def test_rejects_invalid_direct_legacy_endpoint(self, endpoint: _LegacyMongoEndpoint) -> None:
        with pytest.raises(ValidationError, match="invalid or unsupported"):
            MaillardConfig(_env_file=None, mongodb_uri=endpoint)

    @pytest.mark.parametrize(
        "invalid_input",
        [
            _LegacyMongoEndpoint(host=f"{_SENTINEL_PASSWORD}.internal", port=27017),
            MagicMock(name=_SENTINEL_PASSWORD),
        ],
    )
    def test_rejects_direct_legacy_objects_without_retaining_repr(
        self, invalid_input: object
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            MaillardConfig(_env_file=None, mongodb_uri=invalid_input)

        assert "invalid or unsupported" in str(exc_info.value)
        _assert_validation_error_is_secret_safe(exc_info.value, _SENTINEL_PASSWORD)

    @pytest.mark.parametrize(
        ("uri", "expected_host"),
        [
            ("mongodb://mongo-1.internal:27018", "mongo-1.internal"),
            ("mongodb://192.0.2.10:27018", "192.0.2.10"),
            ("mongodb://[2001:db8::10]:27018", "2001:db8::10"),
        ],
    )
    def test_accepts_safe_legacy_hosts(self, uri: str, expected_host: str) -> None:
        config = MaillardConfig(_env_file=None, mongodb_uri=uri)

        assert config.mongodb_host == expected_host
        assert config.mongodb_port == 27018

    @pytest.mark.parametrize(
        "uri",
        [
            f"mongodb://mongo.internal:{_SENTINEL_PASSWORD}",
            f"mongodb://mongo.internal:credentials-{_SENTINEL_PASSWORD}",
            f"mongodb://mongo.internal:27017/?password={_SENTINEL_PASSWORD}",
            f"mongodb://mongo%20{_SENTINEL_PASSWORD}.internal:27017",
            f"mongodb://mongo\n{_SENTINEL_PASSWORD}.internal:27017",
            f"mongodb://mongo-{_SENTINEL_PASSWORD}-é.internal:27017",
        ],
    )
    def test_rejects_malformed_legacy_uri_without_echoing_parser_input(self, uri: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            MaillardConfig(_env_file=None, mongodb_uri=uri)

        assert "invalid or unsupported" in str(exc_info.value)
        _assert_validation_error_is_secret_safe(exc_info.value, _SENTINEL_PASSWORD)

    def test_rejects_legacy_uri_userinfo_without_echoing_secret(self) -> None:
        credential_uri = (
            f"mongodb://{_SENTINEL_USERNAME}:{_SENTINEL_PASSWORD}@mongo.internal:27017"
        )

        with pytest.raises(ValidationError) as exc_info:
            MaillardConfig(_env_file=None, mongodb_uri=credential_uri)

        rendered_error = str(exc_info.value)
        assert "must not contain credentials" in rendered_error
        _assert_validation_error_is_secret_safe(
            exc_info.value, _SENTINEL_USERNAME, _SENTINEL_PASSWORD
        )

    def test_rejects_credential_uri_environment_without_echoing_secret(self) -> None:
        credential_uri = (
            f"mongodb://{_SENTINEL_USERNAME}:{_SENTINEL_PASSWORD}@mongo.internal:27017"
        )

        with (
            patch.dict(os.environ, {"MAILLARD_MONGODB_URI": credential_uri}, clear=False),
            pytest.raises(ValidationError) as exc_info,
        ):
            MaillardConfig(_env_file=None)

        rendered_error = str(exc_info.value)
        assert "must not contain credentials" in rendered_error
        _assert_validation_error_is_secret_safe(
            exc_info.value, _SENTINEL_USERNAME, _SENTINEL_PASSWORD
        )

    def test_rejects_uri_or_userinfo_in_host_without_echoing_input(self) -> None:
        unsafe_host = f"mongodb://{_SENTINEL_PASSWORD}@mongo.internal"

        with pytest.raises(ValidationError) as exc_info:
            MaillardConfig(_env_file=None, mongodb_host=unsafe_host)

        rendered_error = str(exc_info.value)
        assert "hostname or IP characters" in rendered_error
        _assert_validation_error_is_secret_safe(exc_info.value, _SENTINEL_PASSWORD)

    @pytest.mark.parametrize(
        "variable",
        ["MAILLARD_MONGODB_USERNAME", "MAILLARD_MONGODB_PASSWORD"],
    )
    def test_rejects_credential_environment_variables_without_echoing_value(
        self, variable: str
    ) -> None:
        with (
            patch.dict(os.environ, {variable: _SENTINEL_PASSWORD}, clear=False),
            pytest.raises(ValidationError) as exc_info,
        ):
            MaillardConfig(_env_file=None)

        rendered_error = str(exc_info.value)
        assert "credential environment variables are prohibited" in rendered_error
        _assert_validation_error_is_secret_safe(exc_info.value, _SENTINEL_PASSWORD)

    def test_environment_and_process_arguments_contain_paths_not_values(
        self, tmp_path: Path
    ) -> None:
        username_file, password_file = _credential_files(tmp_path)
        settings = {
            "MAILLARD_MONGODB_USERNAME_FILE": str(username_file),
            "MAILLARD_MONGODB_PASSWORD_FILE": str(password_file),
        }

        with patch.dict(os.environ, settings, clear=True):
            config = MaillardConfig(_env_file=None)
            assert config.load_mongodb_credentials() == (
                _SENTINEL_USERNAME,
                _SENTINEL_PASSWORD,
            )
            environment = "\x00".join(f"{key}={value}" for key, value in os.environ.items())
            arguments = "\x00".join(sys.argv)

        assert str(username_file) in environment
        assert str(password_file) in environment
        assert _SENTINEL_USERNAME not in environment
        assert _SENTINEL_PASSWORD not in environment
        assert _SENTINEL_USERNAME not in arguments
        assert _SENTINEL_PASSWORD not in arguments


class TestMongoCredentialRuntime:
    @pytest.mark.asyncio
    async def test_motor_receives_separate_connection_parameters(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import maillard.db as db_module

        username_file, password_file = _credential_files(tmp_path)
        config = MaillardConfig(
            _env_file=None,
            mongodb_host="mongo.apps.svc.cluster.local",
            mongodb_port=27018,
            mongodb_name="maillard",
            mongodb_auth_source="maillard",
            mongodb_username_file=username_file,
            mongodb_password_file=password_file,
        )
        client = MagicMock()
        client.__getitem__.return_value = MagicMock()

        caplog.set_level(logging.INFO, logger="maillard.db")
        with (
            patch.object(db_module, "get_config", return_value=config),
            patch.object(db_module, "AsyncIOMotorClient", return_value=client) as motor,
            patch.object(db_module, "init_beanie", new_callable=AsyncMock),
        ):
            db_module._client = None
            await db_module.init_db()

        motor.assert_called_once_with(
            host="mongo.apps.svc.cluster.local",
            port=27018,
            username=_SENTINEL_USERNAME,
            password=_SENTINEL_PASSWORD,
            authSource="maillard",
        )
        assert _SENTINEL_USERNAME not in caplog.text
        assert _SENTINEL_PASSWORD not in caplog.text
        db_module._client = None

    @pytest.mark.asyncio
    async def test_connection_failure_logs_and_raises_no_secret(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import maillard.db as db_module

        username_file, password_file = _credential_files(tmp_path)
        config = MaillardConfig(
            _env_file=None,
            mongodb_username_file=username_file,
            mongodb_password_file=password_file,
        )
        leaked_exception = ConnectionError(
            f"authentication failed for {_SENTINEL_USERNAME}:{_SENTINEL_PASSWORD}"
        )

        caplog.set_level(logging.WARNING, logger="maillard.db")
        with (
            patch.object(db_module, "get_config", return_value=config),
            patch.object(db_module, "AsyncIOMotorClient", side_effect=leaked_exception),
            patch.object(db_module.asyncio, "sleep", new_callable=AsyncMock),
        ):
            db_module._client = None
            with pytest.raises(RuntimeError, match="MongoDB initialization failed") as exc_info:
                await db_module.init_db()

        all_output = f"{caplog.text} {exc_info.value!s}"
        assert _SENTINEL_USERNAME not in all_output
        assert _SENTINEL_PASSWORD not in all_output

    def test_health_failure_does_not_log_database_exception_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from fastapi.testclient import TestClient

        from maillard.mcp.state import ApplicationState
        from maillard.web.app import create_app

        client = MagicMock()
        client.admin.command = AsyncMock(
            side_effect=ConnectionError(f"authentication failed: {_SENTINEL_PASSWORD}")
        )
        state = ApplicationState()
        state._initialized = True

        caplog.set_level(logging.WARNING, logger="maillard.web.app")
        with patch("maillard.db.get_client", return_value=client):
            response = TestClient(create_app(state), raise_server_exceptions=False).get("/health")

        assert response.status_code == 503
        assert response.json()["mongodb"] == "disconnected"
        assert _SENTINEL_PASSWORD not in response.text
        assert _SENTINEL_PASSWORD not in caplog.text


def test_example_configuration_has_no_secret_bearing_database_environment() -> None:
    example = Path(".env.example").read_text(encoding="utf-8")

    assert "MAILLARD_MONGODB_URI=" not in example
    assert "MAILLARD_MONGODB_USERNAME=" not in example
    assert "MAILLARD_MONGODB_PASSWORD=" not in example
    assert "MAILLARD_MONGODB_USERNAME_FILE=" in example
    assert "MAILLARD_MONGODB_PASSWORD_FILE=" in example
