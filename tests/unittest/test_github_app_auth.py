import copy
import tomllib
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider


class TestGithubAppAuth:
    def test_integer_app_id_produces_valid_jwt(self):
        """GitHub App ids are integers in the settings toml, but PyJWT >=2.11 rejects a
        non-string `iss` claim and PyGithub 1.59 passes the id through raw (#2955,
        previously #2210; fixed upstream in PyGithub#3272, which we don't ship yet).
        The provider must cast to str before building the authentication.
        """
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        settings = get_settings()
        original = {
            "GITHUB.DEPLOYMENT_TYPE": settings.get("GITHUB.DEPLOYMENT_TYPE", None),
            "GITHUB.PRIVATE_KEY": settings.get("GITHUB.PRIVATE_KEY", None),
            "GITHUB.APP_ID": settings.get("GITHUB.APP_ID", None),
        }
        settings.set("GITHUB.DEPLOYMENT_TYPE", "app")
        settings.set("GITHUB.PRIVATE_KEY", private_key_pem)
        settings.set("GITHUB.APP_ID", 123456)  # integer, as toml parses it
        try:
            provider = GithubProvider.__new__(GithubProvider)
            provider.installation_id = 987654
            provider.base_url = "https://api.github.com"
            provider._get_github_client()

            # Signing the app JWT is offline; PyJWT >=2.11 raises
            # "Issuer (iss) must be a string" here without the str() cast.
            token = provider.auth._app_auth.create_jwt()
            claims = jwt.decode(
                token, key.public_key(), algorithms=["RS256"], options={"verify_exp": False}
            )
            assert claims["iss"] == "123456"
        finally:
            for name, value in original.items():
                settings.set(name, value)

    def test_is_bot_user_reads_github_section_setting(self):
        """is_bot_user must honor `ignore_bot_pr` from the [github] section
        (#3017): configuration.toml sets it under [github], so reading
        GITHUB_APP.IGNORE_BOT_PR left the guard permanently off."""
        from pr_agent.servers.github_app import is_bot_user

        settings = get_settings()
        original = settings.get("GITHUB.IGNORE_BOT_PR", None)
        settings.set("GITHUB.IGNORE_BOT_PR", True)
        try:
            assert is_bot_user("some-bot[bot]", "Bot") is True
            assert is_bot_user("human", "User") is False
        finally:
            settings.set("GITHUB.IGNORE_BOT_PR", original)

    def test_is_bot_user_off_when_setting_unset(self):
        """With the setting off, bot senders are not filtered."""
        from pr_agent.servers.github_app import is_bot_user

        settings = get_settings()
        original = settings.get("GITHUB.IGNORE_BOT_PR", None)
        settings.set("GITHUB.IGNORE_BOT_PR", False)
        try:
            assert is_bot_user("some-bot[bot]", "Bot") is False
        finally:
            settings.set("GITHUB.IGNORE_BOT_PR", original)


@pytest.fixture
def bot_pr_settings():
    """Snapshot both sections wholesale: Dynaconf's ``unset`` does not remove a dotted key,
    so a key a test adds can only be dropped by restoring its section."""
    settings = get_settings()
    originals = {name: copy.deepcopy(settings.get(name)) for name in ("GITHUB", "GITHUB_APP")}
    try:
        yield settings
    finally:
        for name, original in originals.items():
            settings.unset(name, force=True)
            settings.set(name, original)


class TestIgnoreBotPrSections:
    def test_option_ships_under_the_github_section(self):
        import pr_agent

        config_path = Path(pr_agent.__file__).parent / "settings" / "configuration.toml"
        with config_path.open("rb") as handle:
            shipped = tomllib.load(handle)

        # Read the file, not the merged settings, so env overrides cannot skew this.
        assert "ignore_bot_pr" in shipped["github"]
        assert "ignore_bot_pr" not in shipped.get("github_app", {})

    def test_legacy_github_app_override_is_honoured(self, bot_pr_settings):
        from pr_agent.servers.github_app import is_bot_user

        bot_pr_settings.set("GITHUB.IGNORE_BOT_PR", True)
        bot_pr_settings.set("GITHUB_APP.IGNORE_BOT_PR", False)

        assert is_bot_user("dependabot[bot]", "Bot") is False
