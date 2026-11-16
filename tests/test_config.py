import pytest


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "[::1]", "10.0.0.1", "172.16.0.1",
    "172.31.255.254", "192.168.1.50", "[fd12:3456::1]",
])
def test_local_http_requires_explicit_opt_in(config, host):
    config.public_origin = f"http://{host}:8081"
    with pytest.raises(ValueError, match="PUBLIC_ORIGIN requires HTTPS"):
        config.validate_transport()
    config.allow_insecure_local = True
    config.validate_transport()


@pytest.mark.parametrize("host", [
    "spacecolonyark", "example.com", "localhost.example.com", "8.8.8.8",
    "172.15.255.255", "172.32.0.1", "0.0.0.0", "169.254.1.1", "192.0.2.1",
    "100.64.0.1", "224.0.0.1", "[::]", "[2001:db8::1]", "[2606:4700:4700::1111]",
])
def test_insecure_opt_in_does_not_allow_other_http_hosts(config, host):
    config.public_origin = f"http://{host}:8081"
    config.allow_insecure_local = True
    with pytest.raises(ValueError, match="PUBLIC_ORIGIN requires HTTPS"):
        config.validate_transport()


def test_https_does_not_require_opt_in(config):
    config.public_origin = "https://spacecolonyark:8081"
    config.validate_transport()


@pytest.mark.parametrize("encoder", ["auto", "software", "hardware", "vaapi", "nvenc"])
def test_encoder_environment_setting(config, monkeypatch, encoder):
    monkeypatch.setenv("VIDEO_ENCODER", encoder)
    settings = type(config)(_env_file=None, **config.model_dump(exclude={"video_encoder"}))
    assert settings.video_encoder == encoder


def test_invalid_encoder_rejected(config, monkeypatch):
    monkeypatch.setenv("VIDEO_ENCODER", "not-an-encoder")
    with pytest.raises(ValueError, match="video_encoder"):
        type(config)(_env_file=None, **config.model_dump(exclude={"video_encoder"}))
