"""Tests for configuration management."""

import os
from unittest.mock import patch

import pytest

from maillard.core.config import MaillardConfig, get_config


class TestMaillardConfig:
    """Tests for MaillardConfig."""

    def test_default_values(self) -> None:
        """Test that default values are set correctly."""
        # Clear ALL MAILLARD_ env vars and prevent .env file loading
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("MAILLARD_")}
        with patch.dict(os.environ, clean_env, clear=True):
            config = MaillardConfig(_env_file=None)

            # Kaleido connection defaults
            assert config.kaleido_host == "192.0.2.10"
            assert config.kaleido_port == 80
            assert config.kaleido_path == "ws"

            # Safety limits
            assert config.max_bt_temp == 230.0
            assert config.max_et_temp == 240.0
            assert config.max_ror == 20.0
            assert config.min_ror == 3.0
            assert config.max_roast_duration == 1200

            # Database
            assert config.mongodb_host == "localhost"
            assert config.mongodb_port == 27017
            assert config.mongodb_name == "maillard"

            # API
            assert config.api_host == "127.0.0.1"
            assert config.api_port == 8080

            # MCP
            assert config.mcp_server_name == "maillard"

            # Logging
            assert config.log_level == "INFO"
            assert config.log_file == "data/logs/maillard.log"

    def test_kaleido_ws_url_property(self) -> None:
        """Test the kaleido_ws_url property."""
        config = MaillardConfig(
            kaleido_host="192.0.2.10",
            kaleido_port=80,
            kaleido_path="ws",
        )
        assert config.kaleido_ws_url == "ws://192.0.2.10:80/ws"

        config = MaillardConfig(
            kaleido_host="10.0.0.50",
            kaleido_port=8080,
            kaleido_path="websocket",
        )
        assert config.kaleido_ws_url == "ws://10.0.0.50:8080/websocket"

    def test_env_prefix(self) -> None:
        """Test that environment variables with MAILLARD_ prefix are read."""
        with patch.dict(
            os.environ,
            {
                "MAILLARD_KALEIDO_HOST": "10.0.0.1",
                "MAILLARD_KALEIDO_PORT": "8888",
                "MAILLARD_MAX_BT_TEMP": "225.0",
                "MAILLARD_LOG_LEVEL": "DEBUG",
            },
        ):
            config = MaillardConfig()
            assert config.kaleido_host == "10.0.0.1"
            assert config.kaleido_port == 8888
            assert config.max_bt_temp == 225.0
            assert config.log_level == "DEBUG"

    def test_extra_fields_ignored(self) -> None:
        """Test that extra fields are ignored."""
        with patch.dict(
            os.environ,
            {"MAILLARD_UNKNOWN_FIELD": "value"},
        ):
            # Should not raise an error
            config = MaillardConfig()
            assert not hasattr(config, "unknown_field")

    def test_type_validation(self) -> None:
        """Test that type validation works."""
        # Valid types should work
        config = MaillardConfig(
            kaleido_port=8080,
            max_bt_temp=220.5,
            max_roast_duration=900,
        )
        assert config.kaleido_port == 8080
        assert config.max_bt_temp == 220.5
        assert config.max_roast_duration == 900

        # String that can be coerced to int should work
        with patch.dict(os.environ, {"MAILLARD_KALEIDO_PORT": "9090"}):
            config = MaillardConfig()
            assert config.kaleido_port == 9090

    def test_invalid_type_raises_error(self) -> None:
        """Test that invalid types raise ValidationError."""
        from pydantic import ValidationError

        # Invalid string for int field
        with patch.dict(os.environ, {"MAILLARD_KALEIDO_PORT": "not_a_number"}):
            with pytest.raises(ValidationError) as exc_info:
                MaillardConfig()
            assert "kaleido_port" in str(exc_info.value)

        # Invalid string for float field
        with patch.dict(os.environ, {"MAILLARD_MAX_BT_TEMP": "very_hot"}):
            with pytest.raises(ValidationError) as exc_info:
                MaillardConfig()
            assert "max_bt_temp" in str(exc_info.value)


class TestGetConfig:
    """Tests for get_config function."""

    def test_returns_config_instance(self) -> None:
        """Test that get_config returns an MaillardConfig instance."""
        # Clear the cache first
        get_config.cache_clear()
        config = get_config()
        assert isinstance(config, MaillardConfig)

    def test_caches_result(self) -> None:
        """Test that get_config caches the result."""
        get_config.cache_clear()
        config1 = get_config()
        config2 = get_config()
        assert config1 is config2
