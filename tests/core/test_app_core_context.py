"""应用初始化、配置和插件上下文。"""

from __future__ import annotations

import tests.helpers.app_test_support as _fixture_support
from tests.helpers.app_test_support import (
    AsyncMock,
    MagicMock,
    Mapping,
    Path,
    XiaoQingApp,
    patch,
    pytest,
)

mock_dependencies = _fixture_support.mock_dependencies
temp_app_root     = _fixture_support.temp_app_root


@pytest.mark.unit
def test_app_init_with_minimal_args(temp_app_root: Path):
    """Test app initialization with minimal arguments"""
    with patch("core.app.setup_logging") as mock_setup:
        mock_setup.return_value = MagicMock()
        app                     = XiaoQingApp(temp_app_root)

        assert app.root == temp_app_root
    assert app.config_manager is not None
    assert app.log_manager is not None
    assert app.http_session is None
    assert app.http_sender is None
    assert app.ws_client is None
    assert app.inbound_manager is None
    assert app.router is not None
    assert app.plugin_manager is not None
    assert app.scheduler is not None
    assert app.metrics is not None
    assert app.session_manager is not None
    assert app.dispatcher is not None


@pytest.mark.unit
def test_app_init_with_dependencies(temp_app_root: Path, mock_dependencies):
    """Test app initialization with injected dependencies"""
    app = XiaoQingApp(
        temp_app_root,
        router          = mock_dependencies["router"],
        plugin_manager  = mock_dependencies["plugin_manager"],
        dispatcher      = mock_dependencies["dispatcher"],
        scheduler       = mock_dependencies["scheduler"],
        session_manager = mock_dependencies["session_manager"],
    )

    assert app.router is mock_dependencies["router"]
    assert app.plugin_manager is mock_dependencies["plugin_manager"]
    assert app.dispatcher is mock_dependencies["dispatcher"]
    assert app.scheduler is mock_dependencies["scheduler"]
    assert app.session_manager is mock_dependencies["session_manager"]


@pytest.mark.unit
def test_app_plugins_dir_path(temp_app_root: Path):
    """Test that plugins_dir is set correctly"""
    app = XiaoQingApp(temp_app_root)
    assert app.plugins_dir == temp_app_root / "plugins"
    assert app.plugin_manager.data_root == temp_app_root / "data"


@pytest.mark.unit
def test_app_resolves_relative_plugin_data_root_from_project(temp_app_root: Path):
    import json

    config_path = temp_app_root / "config" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["data_root"] = "runtime/plugin-data"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    app = XiaoQingApp(temp_app_root)

    assert app.plugin_manager.data_root == temp_app_root / "runtime" / "plugin-data"


@pytest.mark.unit
def test_app_config_property(temp_app_root: Path):
    """Test config property access"""
    app = XiaoQingApp(temp_app_root)

    config = app.config
    assert isinstance(config, Mapping)
    assert config.get("bot_name") == "小青"


@pytest.mark.unit
def test_app_secrets_property(temp_app_root: Path):
    """Test secrets property access"""
    app = XiaoQingApp(temp_app_root)

    secrets = app.secrets
    assert isinstance(secrets, Mapping)
    assert 12345 in secrets.get("admin_user_ids", [])


@pytest.mark.unit
def test_app_is_admin_valid_user(temp_app_root: Path):
    """Test is_admin with valid admin user"""
    app = XiaoQingApp(temp_app_root)

    assert app.is_admin(12345) is True
    assert app.is_admin(67890) is True


@pytest.mark.unit
def test_app_is_admin_invalid_user(temp_app_root: Path):
    """Test is_admin with non-admin user"""
    app = XiaoQingApp(temp_app_root)

    assert app.is_admin(99999) is False
    assert app.is_admin(1) is False


@pytest.mark.unit
def test_app_is_admin_none_user(temp_app_root: Path):
    """Test is_admin with None user_id"""
    app = XiaoQingApp(temp_app_root)

    assert app.is_admin(None) is False
    assert app.is_admin(0) is False


@pytest.mark.parametrize("user_id", [True, False, -1, "12345", 12345.0])
def test_app_is_admin_rejects_non_integer_runtime_values(
    temp_app_root: Path,
    user_id: object,
) -> None:
    app = XiaoQingApp(temp_app_root)

    assert app.is_admin(user_id) is False  # type: ignore[arg-type]


@pytest.mark.unit
def test_app_load_admins_from_config(temp_app_root: Path):
    """Test admin loading from secrets config"""
    import json

    # Modify secrets to have different admin IDs
    secrets_file = temp_app_root / "config" / "secrets.json"
    with open(secrets_file, "w") as f:
        json.dump({"admin_user_ids": ["111", "222", "333"]}, f)

    app = XiaoQingApp(temp_app_root)
    assert app.is_admin(111)
    assert app.is_admin(222)
    assert app.is_admin(333)


@pytest.mark.unit
def test_app_load_admins_invalid_config(temp_app_root: Path):
    """Test admin loading with invalid config"""
    import json

    # Write invalid admin_user_ids
    secrets_file = temp_app_root / "config" / "secrets.json"
    with open(secrets_file, "w") as f:
        json.dump({"admin_user_ids": ["not", "a", "number"]}, f)

    app = XiaoQingApp(temp_app_root)
    # Should have empty admin set and no user should be admin
    assert app.is_admin(12345) is False


@pytest.mark.parametrize(
    "raw_ids",
    [
        "12345",
        True,
        [True],
        [0],
        [-1],
        [12345.0],
        [" 12345"],
        ["12345", False],
        {"12345": True},
    ],
)
def test_app_load_admins_rejects_unsafe_shapes(
    temp_app_root: Path,
    raw_ids: object,
) -> None:
    app = XiaoQingApp(temp_app_root)

    app._load_admins({"admin_user_ids": raw_ids})

    assert app._admin_set == set()


def test_app_load_admins_accepts_positive_ints_and_decimal_strings(
    temp_app_root: Path,
) -> None:
    app = XiaoQingApp(temp_app_root)

    app._load_admins({"admin_user_ids": (12345, "67890", "12345")})

    assert app._admin_set == {12345, 67890}


@pytest.mark.unit
def test_app_build_plugin_context(temp_app_root: Path):
    """Test _build_plugin_context creates valid context"""
    app = XiaoQingApp(temp_app_root)

    plugin_dir = Path("/test/plugin")
    data_dir   = Path("/test/data")
    state      = {"test": "value"}

    context = app._build_plugin_context(
        plugin_name = "test_plugin",
        plugin_dir  = plugin_dir,
        data_dir    = data_dir,
        state       = state,
        user_id     = 12345,
        group_id    = 67890,
        request_id  = "test-request-123",
    )

    assert context.plugin_name == "test_plugin"
    assert context.plugin_dir == plugin_dir
    assert context.data_dir == data_dir
    assert context.state == state
    assert context.current_user_id == 12345
    assert context.current_group_id == 67890
    assert context.request_id == "test-request-123"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_context_send_action(temp_app_root: Path):
    """Test plugin context send_action works"""
    app = XiaoQingApp(temp_app_root)

    context = app._build_plugin_context(
        plugin_name = "test",
        plugin_dir  = Path("/test"),
        data_dir    = Path("/test"),
        state       = {},
    )

    # Mock _send_action to track calls
    with patch.object(app, "_send_action", new=AsyncMock()) as mock_send:
        await context.send_action({"action": "test"})
        mock_send.assert_called_once()
        assert mock_send.await_args.args[0]["_source_plugin"] == "test"


@pytest.mark.unit
def test_app_plugin_context_scopes_and_freezes_config_and_secrets(temp_app_root: Path):
    app = XiaoQingApp(temp_app_root)
    config = dict(app.config_manager.config)
    config["plugins"] = {"alpha": {"enabled": True}, "beta": {"enabled": True}}
    secrets = {"admin_user_ids": [1], "plugins": {"alpha": {"token": "a"}, "beta": {"token": "b"}}}
    app.config_manager._replace_snapshot(config, secrets)

    context = app._build_plugin_context("alpha", Path("/alpha"), Path("/alpha/data"), {})

    assert context.config["plugins"] == {"alpha": {"enabled": True}}
    assert context.secrets == {"plugins": {"alpha": {"token": "a"}}}
    assert context.config_manager is None
    with pytest.raises(TypeError):
        context.config["plugins"]["alpha"]["enabled"] = False
    with pytest.raises(TypeError):
        context.secrets["plugins"]["alpha"]["token"] = "changed"


@pytest.mark.unit
def test_app_plugin_settings_views_are_cached_per_plugin_generation(temp_app_root: Path):
    from core.config import ConfigSnapshot

    app      = XiaoQingApp(temp_app_root)
    first    = app._plugin_settings_snapshot("alpha")
    repeated = app._plugin_settings_snapshot("alpha")

    assert repeated is first
    assert repeated.config is first.config
    assert repeated.secrets is first.secrets

    context = app._build_plugin_context("alpha", Path("/alpha"), Path("/alpha/data"), {})
    assert context.config is first.config
    assert context.secrets is first.secrets

    next_generation = ConfigSnapshot(
        config   = {"plugins": {"alpha": {"generation": "next"}}},
        secrets  = {"plugins": {"alpha": {"generation": "next"}}},
        revision = first.revision + 1,
    )
    refreshed = app._plugin_settings_snapshot("alpha", next_generation)

    assert refreshed is not first
    assert refreshed.config["plugins"]["alpha"]["generation"] == "next"
    assert app._plugin_settings_snapshot("alpha", next_generation) is refreshed


@pytest.mark.unit
@pytest.mark.parametrize("malformed_namespace", ["config", "secrets"])
def test_app_plugin_context_rejects_non_mapping_plugin_namespaces(
    temp_app_root: Path,
    malformed_namespace: str,
):
    from core.config import ConfigSnapshot

    app                    = XiaoQingApp(temp_app_root)
    config_plugins: object = [] if malformed_namespace == "config" else {"alpha": {"enabled": True}}
    secret_plugins: object = [] if malformed_namespace == "secrets" else {"alpha": {"token": "a"}}
    snapshot               = ConfigSnapshot(
        config   = {"plugins": config_plugins},
        secrets  = {"plugins": secret_plugins},
        revision = 7,
    )

    with patch.object(app.config_manager, "snapshot", return_value=snapshot):
        context = app._build_plugin_context(
            "alpha",
            Path("/alpha"),
            Path("/alpha/data"),
            {},
        )

    expected_config  = {} if malformed_namespace == "config" else {"enabled": True}
    expected_secrets = {} if malformed_namespace == "secrets" else {"token": "a"}
    assert context.config["plugins"] == {"alpha": expected_config}
    assert context.secrets["plugins"] == {"alpha": expected_secrets}


@pytest.mark.unit
def test_app_plugin_context_initial_settings_share_one_generation(temp_app_root: Path):
    """Initial config/secrets and later refreshes each come from one snapshot call."""
    from core.config import ConfigSnapshot

    app   = XiaoQingApp(temp_app_root)
    first = ConfigSnapshot(
        config   = {"plugins": {"alpha": {"generation": "old-public"}}},
        secrets  = {"plugins": {"alpha": {"generation": "old-private"}}},
        revision = 11,
    )
    second = ConfigSnapshot(
        config   = {"plugins": {"alpha": {"generation": "new-public"}}},
        secrets  = {"plugins": {"alpha": {"generation": "new-private"}}},
        revision = 12,
    )

    with patch.object(app.config_manager, "snapshot", side_effect=[first, second]) as reader:
        context = app._build_plugin_context(
            "alpha",
            Path("/alpha"),
            Path("/alpha/data"),
            {},
        )

        assert reader.call_count == 1
        assert context.config["plugins"]["alpha"]["generation"] == "old-public"
        assert context.secrets["plugins"]["alpha"]["generation"] == "old-private"

        refreshed = context.get_settings_snapshot()

    assert reader.call_count == 2
    assert refreshed.revision == 12
    assert refreshed.config["plugins"]["alpha"]["generation"] == "new-public"
    assert refreshed.secrets["plugins"]["alpha"]["generation"] == "new-private"
    assert context.config_manager is None
