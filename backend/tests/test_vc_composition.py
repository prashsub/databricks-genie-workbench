from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.services.version_control import contracts, platform
from backend.services.version_control.platform.feature_flags import FeatureFlags, WRITE_SWITCHES


def test_modules_are_wired_once_with_write_switch_default_off():
    compose = getattr(platform, "compose", None)
    assert callable(compose), "Root must install a lazy, explicitly injected VC composition"
    factory = Mock(return_value=object())
    container = compose({contracts.IdentityProvider: factory})
    factory.assert_not_called()
    assert container.resolve(contracts.IdentityProvider) is container.resolve(contracts.IdentityProvider)
    factory.assert_called_once_with()
    with pytest.raises(ValueError, match="already registered"):
        container.register(contracts.IdentityProvider, factory)
    assert all(not container.flags.enabled(name) for name in WRITE_SWITCHES)
    assert not FeatureFlags(vc_restore_enabled=True).enabled("vc_restore_enabled")
    with pytest.raises(KeyError):
        container.resolve(contracts.Coordination)
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "app.state.version_control = compose()" in source
