"""Local service configuration."""

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr
    admin_token: SecretStr = Field(min_length=32)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    pool_min_size: int = Field(default=2, ge=1, le=20)
    pool_max_size: int = Field(default=20, ge=2, le=100)
    pool_timeout: float = Field(default=5.0, gt=0, le=60)

    @model_validator(mode="after")
    def valid_pool_sizes(self):
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("The minimum pool size must not exceed the maximum.")
        return self
