from pydantic_settings import BaseSettings
from typing import ClassVar, Dict, Any
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseSettings):
    # ------------ OPENAI ------------
    OPENAI_MODEL: ClassVar[str] = "gpt-4o-mini"
    OPENAI_API_KEY: str

    # ------------ URL CONFIGURATION ------------
    DEFAULT_BASE_URI: str = "newserver:81"

    GB_API_TIMEOUT: int = 60
    GB_MOCK_MODE: bool = False

    WEEKOFF_DAYS: str = "6"          # 0=Mon … 6=Sun, comma-separated
    HOLIDAY_COUNTRY_CODE: str = "IN"

    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    # ============================================================
    # Dynamic URL Generators (UNCHANGED)
    # ============================================================

    def get_direct_url(self, login_dto: Dict[str, Any] = None) -> str:
        base_url = self.DEFAULT_BASE_URI

        if login_dto and "BaseURL" in login_dto:
            base_url = login_dto["BaseURL"]

        if not base_url.startswith("http"):
            base_url = f"http://{base_url}"

        return base_url.rstrip("/")

    # ============================================================
    # 🔥 MISSING ATTRIBUTE – ADDED (NO LOGIC CHANGE)
    # ============================================================
    @property
    def GB_API_BASE(self) -> str:
        """
        Used by biztransactionid.service
        Maps to direct GB API base
        Example: http://newserver:81/gb4
        """
        return self.get_direct_url()

    @property
    def GB_LOGIN_DTO(self) -> Dict[str, Any]:
        """
        Default fallback login used when no Login header is provided.
        In production a real Login header should always be sent by the UI.
        """
        return {
            "UserId": 0,
            "UserName": "System",
            "UserCode": "SYSTEM",
            "WorkOUId": 0,
            "WorkPeriodId": 0,
            "BaseURL": self.DEFAULT_BASE_URI,
        }


settings = Settings()
