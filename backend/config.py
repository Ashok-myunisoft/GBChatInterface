from pydantic_settings import BaseSettings
from typing import ClassVar, Dict, Any
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseSettings):
    # ------------ OPENAI ------------
    OPENAI_MODEL: ClassVar[str] = "gpt-4o-mini"
    OPENAI_API_KEY: str

    # ------------ URL CONFIGURATION ------------
    GB_CONTEXT: str = "gb4"
    GB_PROXY_SCRIPT: str = "apps/proxy.php?url="

    DEFAULT_FE_URI: str = "http://183.82.250.223:92/"
    DEFAULT_BASE_URI: str = "newserver:81"

    GB_API_TIMEOUT: int = 60
    GB_MOCK_MODE: bool = False

    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    # ============================================================
    # Dynamic URL Generators (UNCHANGED)
    # ============================================================

    def get_direct_url(self, login_dto: Dict[str, Any] = None) -> str:
        base_uri = self.DEFAULT_BASE_URI

        if login_dto and "BaseUri" in login_dto:
            base_uri = login_dto["BaseUri"]

        if not base_uri.startswith("http"):
            base_uri = f"http://{base_uri}"

        base_uri = base_uri.rstrip("/")

        return f"{base_uri}/{self.GB_CONTEXT}"

    def get_proxy_url(self, login_dto: Dict[str, Any] = None) -> str:
        fe_uri = self.DEFAULT_FE_URI

        if login_dto and "FEUri" in login_dto:
            fe_uri = login_dto["FEUri"]

        if not fe_uri.endswith("/"):
            fe_uri += "/"

        target_url = self.get_direct_url(login_dto)

        return f"{fe_uri}{self.GB_PROXY_SCRIPT}{target_url}"

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


settings = Settings()
