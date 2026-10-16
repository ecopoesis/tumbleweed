"""Shared pytest setup: the host-capability gate and TUMBLEWEED_CI skip-guard (see the plugin)."""

pytest_plugins = ["pytester", "tests.plugins.host"]
