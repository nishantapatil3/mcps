"""Startup configuration: env parsing, CLI overrides, and safe defaults."""

from __future__ import annotations

import pytest

from location_tools import config, server


# --- Environment parsing ---


def test_home_parses_coordinates(monkeypatch):
    monkeypatch.setenv("LOCATION_TOOLS_HOME", "48.8584,2.2945")
    assert config.from_env().home == (48.8584, 2.2945)


def test_unset_home_is_none(monkeypatch):
    monkeypatch.delenv("LOCATION_TOOLS_HOME", raising=False)
    assert config.from_env().home is None


def test_invalid_home_warns_and_falls_back(monkeypatch, capsys):
    # A bad value must not crash startup; it degrades to IP geolocation.
    monkeypatch.setenv("LOCATION_TOOLS_HOME", "not-a-coordinate")
    settings = config.from_env()
    assert settings.home is None
    assert "invalid LOCATION_TOOLS_HOME" in capsys.readouterr().err


def test_out_of_range_home_warns(monkeypatch, capsys):
    monkeypatch.setenv("LOCATION_TOOLS_HOME", "95.0,0.0")
    assert config.from_env().home is None
    assert "invalid LOCATION_TOOLS_HOME" in capsys.readouterr().err


def test_ssl_verify_defaults_on(monkeypatch):
    monkeypatch.delenv("LOCATION_TOOLS_SSL_VERIFY", raising=False)
    monkeypatch.delenv("LOCATION_TOOLS_CA_CERTS", raising=False)
    assert config.from_env().ssl_verify is True


def test_ssl_verify_can_be_disabled(monkeypatch):
    monkeypatch.setenv("LOCATION_TOOLS_SSL_VERIFY", "0")
    assert config.from_env().ssl_verify is False


def test_ca_certs_path_is_used(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("LOCATION_TOOLS_CA_CERTS", str(bundle))
    assert config.from_env().ssl_verify == str(bundle)


def test_missing_ca_bundle_warns(monkeypatch, capsys):
    monkeypatch.setenv("LOCATION_TOOLS_CA_CERTS", "/nonexistent/ca.pem")
    config.from_env()
    assert "does not exist" in capsys.readouterr().err


def test_invalid_rpm_falls_back_with_a_warning(monkeypatch, capsys):
    monkeypatch.setenv("LOCATION_TOOLS_REQUESTS_PER_MINUTE", "not-a-number")
    assert config.from_env().requests_per_minute == 60
    assert "invalid" in capsys.readouterr().err


def test_rpm_floor_is_one(monkeypatch):
    monkeypatch.setenv("LOCATION_TOOLS_REQUESTS_PER_MINUTE", "0")
    assert config.from_env().requests_per_minute == 1


def test_nominatim_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("LOCATION_TOOLS_NOMINATIM_URL", "https://geo.internal/")
    assert config.from_env().nominatim_url == "https://geo.internal"


def test_cache_ttl_zero_is_allowed(monkeypatch):
    # 0 is meaningful (caching off), so it must not be replaced by the default.
    monkeypatch.setenv("LOCATION_TOOLS_CACHE_TTL", "0")
    assert config.from_env().cache_ttl == 0.0


# --- override() ---


def test_override_ignores_none(monkeypatch):
    monkeypatch.setattr(config, "settings", config.Settings(requests_per_minute=42))
    result = config.override(requests_per_minute=None, cache_ttl=10.0)
    assert result.requests_per_minute == 42
    assert result.cache_ttl == 10.0


# --- CLI ---


def test_cli_home_coordinates_do_not_require_network(monkeypatch):
    args = server._parse_args(["--home", "48.8584,2.2945"])
    assert args.home == "48.8584,2.2945"
    coords, label = server._resolve_home_arg("48.8584,2.2945")
    assert coords == (48.8584, 2.2945)
    assert label == ""


def test_cli_home_place_name_is_geocoded(monkeypatch):
    monkeypatch.setattr(
        server.places,
        "resolve_place",
        lambda q: {"latitude": 35.0, "longitude": 135.75, "short_label": "Kyoto, Japan"},
    )
    coords, label = server._resolve_home_arg("Kyoto")
    assert coords == (35.0, 135.75)
    assert label == "Kyoto, Japan"


def test_cli_home_geocode_failure_degrades_gracefully(monkeypatch, capsys):
    # A transient Nominatim outage must not prevent the server from starting.
    def boom(query):
        raise server.UpstreamError("nominatim unreachable")

    monkeypatch.setattr(server.places, "resolve_place", boom)
    coords, label = server._resolve_home_arg("Kyoto")
    assert coords is None
    assert "falling back to IP geolocation" in capsys.readouterr().err


def test_cli_flags_parse():
    args = server._parse_args(
        [
            "--home",
            "0,0",
            "--home-label",
            "Null Island",
            "--requests-per-minute",
            "10",
            "--cache-ttl",
            "30",
            "--no-ssl-verify",
            "--nominatim-url",
            "https://geo.internal",
        ]
    )
    assert args.home_label == "Null Island"
    assert args.requests_per_minute == 10
    assert args.cache_ttl == 30.0
    assert args.no_ssl_verify is True
    assert args.nominatim_url == "https://geo.internal"


@pytest.mark.parametrize("flag", ["--help"])
def test_help_exits_cleanly(flag):
    with pytest.raises(SystemExit) as exc:
        server._parse_args([flag])
    assert exc.value.code == 0
