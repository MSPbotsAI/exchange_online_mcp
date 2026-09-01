from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": the runtime injects environment variables this project does
    # not declare; rejecting them would crash the container at startup.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mcp_http_port: int = 8080
    mcp_http_host: str = "0.0.0.0"

    # Exchange Online admin endpoint host. Overridable for sovereign clouds
    # (GCC High, DoD, China 21Vianet) and to point tests at a local mock.
    exo_base_url: str = "https://outlook.office365.com"
    # Entra ID login host used for the app-only token exchange. Same reasons.
    entra_login_base_url: str = "https://login.microsoftonline.com"

    # No credential fields live here by design: tenant id, client id and the
    # certificate arrive per-request in HTTP headers (see server.py).


def get_settings() -> Settings:
    return Settings()
