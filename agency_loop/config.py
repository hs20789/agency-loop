"""Runtime settings from environment variables (prefix AGENCY_). API keys are read only by provider clients."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

PROVIDER_MODES = ("mock", "live", "off")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _mode(name: str, default: str = "mock") -> str:
    value = _env(name, default).lower()
    if value not in PROVIDER_MODES:
        raise SystemExit(f"{name} must be one of {PROVIDER_MODES}")
    return value


@dataclass(frozen=True)
class Settings:
    # Web
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:8000", "localhost:8000")
    max_upload_bytes: int = 32 * 1024 * 1024  # 33,554,432 bytes
    workspace_ttl_sec: int = 3600  # (초기값) 미사용 60분 뒤 삭제
    max_workspaces: int = 50  # (초기값)
    upload_rate_per_10min: int = 10  # (초기값) per client IP
    evidence_excerpt_chars: int = 4000
    bundle_max_chars: int = 12000  # (초기값)

    # Provider modes: mock | live | off
    anthropic_mode: str = "mock"
    clova_mode: str = "mock"  # HCX-007 review and reranker
    mistral_mode: str = "mock"  # DeepSeek review
    embed_mode: str = "mock"  # BGE-M3 on local-rag
    local_llm_mode: str = "mock"  # MCP summarize_text on local-llm-chat

    # Claude (Q12); Ⓔ report: development-process report added with the Agency Mirror report feature
    claude_model: str = "claude-opus-5-5"
    claude_effort: dict[str, str] = field(
        default_factory=lambda: {
            "analysis": "high", "report": "high", "whatif": "high", "feedback1": "medium", "feedback2": "medium",
        }
    )
    claude_max_tokens: dict[str, int] = field(
        default_factory=lambda: {
            "analysis": 32000, "report": 24000, "whatif": 24000, "feedback1": 12000, "feedback2": 12000,
        }
    )
    claude_attempts_per_flow: int = 7  # 5 model calls per flow + 2 spare retries (PROJECT.md 부록 C)
    claude_daily_attempts: int = 300  # (초기값)
    anthropic_base_url: str = "https://api.anthropic.com"  # explicit, so ANTHROPIC_BASE_URL in the env is never picked up; tests point it at a mock

    # CLOVA
    clova_base_url: str = "https://clovastudio.stream.ntruss.com"
    hcx_model: str = "HCX-007"
    hcx_thinking_effort: str = "low"  # (초기값) measure on day 1
    hcx_max_completion_tokens: int = 5120  # (초기값)
    clova_daily_calls: int = 300  # (초기값)

    # Mistral / DeepSeek (Q13, Q18)
    mistral_base_url: str = "https://api.mistral.ai"
    mistral_model: str = "deepseek-v4-1-flash-1017"
    mistral_max_tokens: int = 8192  # (초기값) always sent explicitly
    mistral_cap_micro_usd: int = 12_000_000  # app cumulative cap $12 pre-tax (Q18)
    mistral_price_in: int = 300_000  # micro-USD per 1M input tokens
    mistral_price_out: int = 1_200_000  # micro-USD per 1M output tokens
    mistral_out_ub_tokens: int = 128_000  # reserve until a test shows max_tokens bounds reasoning
    ledger_path: str = "var/usage.jsonl"

    # Class servers (Q11: existing public-IP addresses, plain HTTP)
    ollama_embed_url: str = ""  # e.g. http://<local-rag public IP>:11434
    embed_model: str = "bge-m3"
    rag_min_requests: int = 6  # (초기값) search only when there are 6+ human requests
    rag_top_k: int = 5  # (초기값)
    rag_min_similarity: float = 0.35  # (초기값) set by measurement
    mcp_command: str = "/root/day05/local-llm-mcp/.venv/bin/python"
    mcp_args: tuple[str, ...] = ("/root/day05/local-llm-mcp/server.py",)
    mcp_cwd: str = "/root/day05/local-llm-mcp"
    mcp_url: str = ""  # e.g. http://<local-llm-chat public IP>:9001/mcp; when set, used instead of the stdio command
    ollama_chat_url: str = ""  # passed to the MCP child as OLLAMA_BASE_URL
    local_llm_model: str = "qwen3.5:9b-q8_0"  # target; final choice by measurement (Q10)
    mcp_timeout_sec: int = 240

    # Server-wide limits (초기값)
    analysis_concurrency: int = 2
    local_llm_concurrency: int = 1

    @classmethod
    def from_env(cls) -> Settings:
        port = _int("AGENCY_PORT", 8000)
        hosts = _env("AGENCY_ALLOWED_HOSTS", f"127.0.0.1:{port},localhost:{port}")
        return cls(
            host=_env("AGENCY_HOST", "127.0.0.1"),
            port=port,
            allowed_hosts=tuple(h.strip() for h in hosts.split(",") if h.strip()),
            workspace_ttl_sec=_int("AGENCY_WORKSPACE_TTL_SEC", 3600),
            max_workspaces=_int("AGENCY_MAX_WORKSPACES", 50),
            upload_rate_per_10min=_int("AGENCY_UPLOAD_RATE_PER_10MIN", 10),
            bundle_max_chars=_int("AGENCY_BUNDLE_MAX_CHARS", 12000),
            anthropic_mode=_mode("AGENCY_ANTHROPIC_MODE"),
            clova_mode=_mode("AGENCY_CLOVA_MODE"),
            mistral_mode=_mode("AGENCY_MISTRAL_MODE"),
            embed_mode=_mode("AGENCY_EMBED_MODE"),
            local_llm_mode=_mode("AGENCY_LOCAL_LLM_MODE"),
            claude_model=_env("AGENCY_CLAUDE_MODEL", "claude-opus-5-5"),
            claude_daily_attempts=_int("AGENCY_CLAUDE_DAILY_ATTEMPTS", 300),
            anthropic_base_url=_env("AGENCY_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            clova_base_url=_env("AGENCY_CLOVA_BASE_URL", "https://clovastudio.stream.ntruss.com"),
            hcx_thinking_effort=_env("AGENCY_HCX_THINKING_EFFORT", "low"),
            hcx_max_completion_tokens=_int("AGENCY_HCX_MAX_COMPLETION_TOKENS", 5120),
            clova_daily_calls=_int("AGENCY_CLOVA_DAILY_CALLS", 300),
            mistral_base_url=_env("AGENCY_MISTRAL_BASE_URL", "https://api.mistral.ai"),
            mistral_model=_env("AGENCY_MISTRAL_MODEL", "deepseek-v4-1-flash-1017"),
            mistral_max_tokens=_int("AGENCY_MISTRAL_MAX_TOKENS", 8192),
            mistral_cap_micro_usd=_int("AGENCY_MISTRAL_CAP_MICRO_USD", 12_000_000),
            mistral_out_ub_tokens=_int("AGENCY_MISTRAL_OUT_UB_TOKENS", 128_000),
            ledger_path=_env("AGENCY_LEDGER_PATH", "var/usage.jsonl"),
            ollama_embed_url=_env("AGENCY_OLLAMA_EMBED_URL", ""),
            rag_min_requests=_int("AGENCY_RAG_MIN_REQUESTS", 6),
            rag_top_k=_int("AGENCY_RAG_TOP_K", 5),
            rag_min_similarity=_float("AGENCY_RAG_MIN_SIMILARITY", 0.35),
            mcp_command=_env("AGENCY_MCP_COMMAND", "/root/day05/local-llm-mcp/.venv/bin/python"),
            mcp_args=tuple(_env("AGENCY_MCP_ARGS", "/root/day05/local-llm-mcp/server.py").split()),
            mcp_cwd=_env("AGENCY_MCP_CWD", "/root/day05/local-llm-mcp"),
            mcp_url=_env("AGENCY_MCP_URL", ""),
            ollama_chat_url=_env("AGENCY_OLLAMA_CHAT_URL", ""),
            local_llm_model=_env("AGENCY_LOCAL_LLM_MODEL", "qwen3.5:9b-q8_0"),
            mcp_timeout_sec=_int("AGENCY_MCP_TIMEOUT_SEC", 240),
        )
