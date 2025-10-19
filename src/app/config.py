from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@db:5432/chat"
    REDIS_URL: str = "redis://redis:6379/0"
    UPLOAD_DIR: str = "/data/uploads"

    DEFAULT_MSG_RATE_RPS: int = 5
    DEFAULT_UPLOAD_BPS: int = 262_144   # 256KB/s
    DEFAULT_DOWNLOAD_BPS: int = 524_288 # 512KB/s
    DEFAULT_BURST: int = 10

    DEV_MODE: bool = True
    SECRET_KEY: str = "change-me"

settings = Settings()
