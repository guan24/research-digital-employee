from __future__ import annotations

from app import config


def test_settings_load_project_root_env_independent_of_working_directory(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    working_dir = tmp_path / "elsewhere"
    project_root.mkdir()
    working_dir.mkdir()
    (project_root / ".env").write_text(
        "\n".join(
            [
                "GITHUB_PERSONAL_ACCESS_TOKEN=project-token",
                "GITHUB_MCP_TRANSPORT=remote",
                "GITHUB_MCP_REMOTE_URL=https://api.githubcopilot.com/mcp/readonly",
                "GITHUB_MCP_TIMEOUT_SECONDS=37",
                "GITHUB_MCP_MAX_RESTARTS=4",
            ]
        ),
        encoding="utf-8",
    )
    (working_dir / ".env").write_text(
        "GITHUB_PERSONAL_ACCESS_TOKEN=wrong-working-directory-token\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)
    monkeypatch.chdir(working_dir)
    for name in (
        "APP_HOME",
        "DATA_DIR",
        "WORKSPACE_DIR",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GITHUB_MCP_TRANSPORT",
        "GITHUB_MCP_REMOTE_URL",
        "GITHUB_MCP_TIMEOUT_SECONDS",
        "GITHUB_MCP_MAX_RESTARTS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = config.Settings.from_env()

    assert settings.app_home == project_root
    assert settings.github_personal_access_token == "project-token"
    assert settings.github_mcp_transport == "remote"
    assert settings.github_mcp_remote_url == "https://api.githubcopilot.com/mcp/readonly"
    assert settings.github_mcp_timeout_seconds == 37.0
    assert settings.github_mcp_max_restarts == 4


def test_process_environment_overrides_project_root_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text(
        "GITHUB_PERSONAL_ACCESS_TOKEN=dotenv-token\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "deployment-secret")
    for name in ("APP_HOME", "DATA_DIR", "WORKSPACE_DIR"):
        monkeypatch.delenv(name, raising=False)

    settings = config.Settings.from_env()

    assert settings.github_personal_access_token == "deployment-secret"
