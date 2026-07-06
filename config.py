"""Configuration management for Zoho automation agents."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
from pathlib import Path

# Get absolute path to .env file (project root)
_PROJECT_ROOT = Path(__file__).parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False
    )

    # Zoho API (Desk)
    zoho_client_id: str
    zoho_client_secret: str
    zoho_refresh_token: str
    zoho_datacenter: str = "com"

    # Zoho Desk
    zoho_desk_org_id: str
    # Emails par département pour les réponses (fromEmailAddress API)
    zoho_desk_email_doc: Optional[str] = None      # DOC department
    zoho_desk_email_contact: Optional[str] = None  # Contact department
    zoho_desk_email_compta: Optional[str] = None   # Comptabilité department
    zoho_desk_email_relations: Optional[str] = "relations.entreprises@cab-formations.fr"
    zoho_desk_email_default: Optional[str] = None  # Fallback
    zoho_desk_relations_department_id: str = "198709000027921097"

    # Internal PlanBot API (Edusign service, read-only)
    planbot_api_url: Optional[str] = None
    planbot_api_secret: Optional[str] = None

    # Zoho CRM (credentials séparées si nécessaire)
    zoho_crm_client_id: Optional[str] = None
    zoho_crm_client_secret: Optional[str] = None
    zoho_crm_refresh_token: Optional[str] = None

    # Anthropic
    anthropic_api_key: str

    # Agent configuration
    agent_model: str = "claude-sonnet-4-5-20250929"  # Legacy — use src.constants.models instead
    agent_max_tokens: int = 4096
    agent_temperature: float = 0.7

    # Staff & escalation
    escalation_agent_id: str = "198709000096599317"
    escalation_agent_name: str = "Lamia Serbouty"
    rgpd_referent_email: str = "jc@cab-formations.fr"

    # Render
    render_api_key: Optional[str] = None

    # Logging
    log_level: str = "INFO"

    @property
    def zoho_accounts_url(self) -> str:
        """Get Zoho accounts URL based on datacenter."""
        return f"https://accounts.zoho.{self.zoho_datacenter}"

    @property
    def zoho_desk_api_url(self) -> str:
        """Get Zoho Desk API URL based on datacenter."""
        return f"https://desk.zoho.{self.zoho_datacenter}/api/v1"

    @property
    def zoho_crm_api_url(self) -> str:
        """Get Zoho CRM API URL based on datacenter."""
        return f"https://www.zohoapis.{self.zoho_datacenter}/crm/v3"


# Global settings instance
settings = Settings()
