"""Configuration management for Maillard."""

from __future__ import annotations

import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_MAX_MONGODB_CREDENTIAL_BYTES = 4096
_MONGODB_HOST_PATTERN = re.compile(r"[A-Za-z0-9._:\[\]-]+")


class _CredentialBearingMongoUriError(ValueError):
    """Internal classification for URI userinfo without retaining it in errors."""


class _InvalidLegacyMongoUriError(ValueError):
    """Internal classification for all non-credential legacy URI failures."""


@dataclass(frozen=True)
class _LegacyMongoEndpoint:
    """A validated, non-secret endpoint parsed from the legacy URI setting."""

    host: str
    port: int


def _is_valid_mongodb_host(value: str) -> bool:
    """Return whether a host contains only non-secret endpoint syntax."""
    return bool(value and _MONGODB_HOST_PATTERN.fullmatch(value))


def _parse_legacy_mongodb_uri(raw_uri: str) -> _LegacyMongoEndpoint:
    """Parse a narrowly supported legacy URI using constant internal errors."""
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in raw_uri
    ):
        raise _InvalidLegacyMongoUriError
    try:
        parsed = urlparse(raw_uri)
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
    except (UnicodeError, ValueError):
        raise _InvalidLegacyMongoUriError from None

    if username is not None or password is not None:
        raise _CredentialBearingMongoUriError
    if (
        parsed.scheme != "mongodb"
        or not hostname
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not _is_valid_mongodb_host(hostname)
    ):
        raise _InvalidLegacyMongoUriError
    try:
        parsed_port = parsed.port
    except ValueError:
        raise _InvalidLegacyMongoUriError from None
    port = 27017 if parsed_port is None else parsed_port
    if not 1 <= port <= 65535:
        raise _InvalidLegacyMongoUriError
    return _LegacyMongoEndpoint(host=hostname, port=port)


class MaillardConfig(BaseSettings):
    """Configuration settings for Maillard."""

    # Kaleido M10 Connection
    kaleido_host: str = "192.0.2.10"
    kaleido_port: int = 80
    kaleido_path: str = "ws"

    # Safety Limits (Celsius)
    max_bt_temp: float = 230.0
    max_et_temp: float = 240.0  # Observed ET <=200C; 40C alarm headroom on this probe.
    max_ror: float = 20.0
    min_ror: float = 3.0
    max_roast_duration: int = 1200  # seconds (20 minutes)

    preheat_timeout_seconds: float = Field(default=1800, gt=0)

    # Watchdog (Claude disconnection detection)
    watchdog_warning_seconds: int = 30  # Warn after this long without tool call
    watchdog_emergency_seconds: int = 60  # Emergency stop after this long (AI auto/MCP only)

    # AI control. Manual is the default; AI auto control (AI writes heat/fan)
    # must be explicitly enabled with MAILLARD_AI_AUTO_CONTROL_ENABLED=true.
    ai_auto_control_enabled: bool = False
    ai_live_model: str = "claude-haiku-4-5-20251001"
    ai_plan_model: str = "claude-sonnet-4-5-20250929"

    # Database (MongoDB). Credentials are read from mounted files at connection
    # time so they never become normal settings values or environment variables.
    mongodb_host: str = "localhost"
    mongodb_port: int = Field(default=27017, ge=1, le=65535)
    mongodb_name: str = "maillard"
    mongodb_auth_source: str = "admin"
    mongodb_username_file: Path | None = None
    mongodb_password_file: Path | None = None

    # Compatibility for the historic, non-secret URI setting. It is deliberately
    # hidden from repr/serialization and rejected if it contains userinfo.
    mongodb_uri: SecretStr | None = Field(default=None, repr=False, exclude=True)

    # API / Web Server
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    web_enabled: bool = True

    site_url: str = "https://roastery.example.com"
    ingest_secret: SecretStr | None = Field(default=None, repr=False)
    ingest_secret_file: Path | None = None
    site_sync_interval_seconds: float = Field(default=15.0, gt=0)

    # MCP Server
    mcp_server_name: str = "maillard"

    # Monitoring
    poll_interval: float = 1.5  # Seconds between state polls during roast

    # Label Printing (Zebra ZD421)
    zebra_host: str = ""  # Empty = TCP printing disabled
    zebra_transport: Literal["auto", "cups", "tcp"] = "auto"
    zebra_port: int = 9100
    zebra_cups_name: str = ""  # CUPS printer name (preferred over TCP)
    zebra_cups_darkness: int = 25  # CUPS print darkness 1-30
    label_base_url: str = ""  # Externally-reachable URL for QR codes
    label_styles_dir: str = ""  # Custom label styles directory (YAML files)
    label_default_style: str = "drop"  # Default label style ID

    # CORS
    cors_origins: str = ""

    # Logging
    log_level: str = "INFO"
    log_format: str = "text"  # "text" or "json"
    log_file: str = "data/logs/maillard.log"
    kaleido_ping_interval: float = Field(default=5.0, ge=0)
    kaleido_ping_timeout: float = Field(default=10.0, ge=0)
    stale_alarm_seconds: float = Field(default=15.0, ge=5)
    alarm_clear_seconds: float = Field(default=5.0, ge=0)

    model_config = SettingsConfigDict(
        env_prefix="MAILLARD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    @model_validator(mode="before")
    @classmethod
    def mask_sensitive_database_validation_inputs(cls, data: object) -> object:
        """Mask legacy URI input and replace an unsafe direct host before errors."""
        if not isinstance(data, dict):
            return data
        raw_uri = data.get("mongodb_uri")
        if isinstance(raw_uri, str):
            data["mongodb_uri"] = SecretStr(raw_uri)
        elif raw_uri is not None and not isinstance(raw_uri, SecretStr):
            data["mongodb_uri"] = SecretStr("invalid-legacy-mongodb-uri-input")
        raw_host = data.get("mongodb_host")
        if isinstance(raw_host, str) and not _is_valid_mongodb_host(raw_host):
            data["mongodb_host"] = ""
        return data

    @field_validator("mongodb_host")
    @classmethod
    def validate_mongodb_host(cls, value: str) -> str:
        """Reject URI/userinfo and log-injection syntax in the non-secret host."""
        if not _is_valid_mongodb_host(value):
            raise ValueError("MongoDB host must contain only hostname or IP characters")
        return value

    @field_validator("mongodb_name", "mongodb_auth_source")
    @classmethod
    def validate_mongodb_database_name(cls, value: str) -> str:
        """Keep database identifiers safe for clients and structured logs."""
        if not value or re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None:
            raise ValueError("MongoDB database identifiers contain unsupported characters")
        return value

    @model_validator(mode="after")
    def validate_mongodb_configuration(self) -> MaillardConfig:
        """Validate file credentials and safely translate a legacy URI."""
        if "MAILLARD_MONGODB_USERNAME" in os.environ or "MAILLARD_MONGODB_PASSWORD" in os.environ:
            raise ValueError(
                "MongoDB credential environment variables are prohibited; use credential files"
            )

        has_username_file = self.mongodb_username_file is not None
        has_password_file = self.mongodb_password_file is not None
        if has_username_file != has_password_file:
            raise ValueError("MongoDB username and password files must be configured together")

        if self.mongodb_uri is None:
            return self

        try:
            endpoint = _parse_legacy_mongodb_uri(self.mongodb_uri.get_secret_value())
        except _CredentialBearingMongoUriError:
            raise ValueError("Legacy MongoDB URI must not contain credentials") from None
        except _InvalidLegacyMongoUriError:
            raise ValueError("Legacy MongoDB URI is invalid or unsupported") from None

        try:
            safe_host = self.validate_mongodb_host(endpoint.host)
        except ValueError:
            raise ValueError("Legacy MongoDB URI is invalid or unsupported") from None
        if not 1 <= endpoint.port <= 65535:
            raise ValueError("Legacy MongoDB URI is invalid or unsupported")
        object.__setattr__(self, "mongodb_host", safe_host)
        object.__setattr__(self, "mongodb_port", endpoint.port)
        return self

    def load_mongodb_credentials(self) -> tuple[str | None, str | None]:
        """Read bounded MongoDB credentials from files without retaining them."""
        if self.mongodb_username_file is None or self.mongodb_password_file is None:
            return None, None
        return (
            self._read_credential_file(self.mongodb_username_file, "username"),
            self._read_credential_file(self.mongodb_password_file, "password"),
        )

    @staticmethod
    def _read_credential_file(path: Path, label: str) -> str:
        """Read one opened, validated secret target with bounded safe errors."""
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            raise ValueError(f"MongoDB {label} file could not be read") from None

        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"MongoDB {label} file must be a regular file")

            permissions = stat.S_IMODE(metadata.st_mode)
            forbidden_permissions = permissions & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            forbidden_permissions |= permissions & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            forbidden_permissions |= permissions & (stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH)
            forbidden_permissions |= permissions & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
            if not permissions & stat.S_IRUSR or forbidden_permissions:
                raise ValueError(f"MongoDB {label} file permissions are not secure")

            current_euid = os.geteuid()
            if metadata.st_uid not in (0, current_euid):
                raise ValueError(f"MongoDB {label} file owner is not trusted")

            if permissions & stat.S_IRGRP:
                process_groups = {os.getegid(), *os.getgroups()}
                if metadata.st_gid not in process_groups:
                    raise ValueError(f"MongoDB {label} file group is not trusted")

            credential_bytes = bytearray()
            while len(credential_bytes) < _MAX_MONGODB_CREDENTIAL_BYTES + 1:
                chunk = os.read(
                    fd,
                    _MAX_MONGODB_CREDENTIAL_BYTES + 1 - len(credential_bytes),
                )
                if not chunk:
                    break
                credential_bytes.extend(chunk)
            raw = bytes(credential_bytes)
        except OSError:
            raise ValueError(f"MongoDB {label} file could not be read") from None
        finally:
            with suppress(OSError):
                os.close(fd)

        if len(raw) > _MAX_MONGODB_CREDENTIAL_BYTES:
            raise ValueError(f"MongoDB {label} file is too large")
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"MongoDB {label} file is not valid UTF-8") from None

        # Kubernetes Secret projections may end in one newline. Preserve all
        # other characters because spaces can be valid password material.
        value = value.removesuffix("\n").removesuffix("\r")
        if not value or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"MongoDB {label} file has invalid content")
        return value

    @property
    def kaleido_ws_url(self) -> str:
        """Get the full WebSocket URL for Kaleido M10."""
        return f"ws://{self.kaleido_host}:{self.kaleido_port}/{self.kaleido_path}"


@lru_cache
def get_config() -> MaillardConfig:
    """Get the cached configuration instance."""
    return MaillardConfig()


def reset_config() -> None:
    """Reset the cached configuration.

    Useful for testing with different configurations.
    """
    get_config.cache_clear()


def get_ingest_secret(config: MaillardConfig) -> str | None:
    """Read the mounted secret at use time; file overrides environment."""
    if config.ingest_secret_file is not None:
        try:
            return config.ingest_secret_file.read_text().strip() or None
        except OSError:
            raise ValueError("MAILLARD_INGEST_SECRET_FILE could not be read") from None
    return config.ingest_secret.get_secret_value() if config.ingest_secret else None
