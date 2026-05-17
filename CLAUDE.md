# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install
pip install .                    # from source
pip install -e .                 # editable install (recommended for dev)

# CLI entry
tradingagents                    # installed command
python -m cli.main               # run from source

# Tests (pytest)
pytest                                          # all tests
pytest -m unit                                  # fast isolated unit tests
pytest -m integration                           # tests requiring external services
pytest tests/test_memory_log.py                 # single file
pytest tests/test_env_overrides.py -x -v        # verbose, stop on first failure

# Codebase checks
ruff check .                     # lint
ruff format --check .            # formatting check
```

## Architecture

### Pipeline Flow

The analysis runs as a **LangGraph pipeline** with this sequence:

1. **Analyst Team** (parallel tool-using agents, configured at init)
   - Market Analyst, Sentiment Analyst, News Analyst, Fundamentals Analyst
   - Each agent calls data tools (stock data, indicators, news, fundamentals)
   - Each can be included/excluded independently

2. **Research Team** (structured debate loop)
   - Bull Researcher and Bear Researcher debate in alternating rounds
   - Research Manager evaluates and produces a structured `ResearchPlan`

3. **Trader Agent** — translates the research plan into a concrete `TraderProposal`

4. **Risk Management Team** (3-way debate loop)
   - Aggressive, Conservative, and Neutral analysts debate the proposal
   - Portfolio Manager makes the final decision via structured `PortfolioDecision`

5. **Reflection Phase** (deferred, on next same-ticker run)
   - Fetches actual returns, computes alpha vs benchmark (SPY or regional index)
   - Generates a reflection injected into future Portfolio Manager prompts

### Key Files

| File | Role |
|------|------|
| `tradingagents/graph/trading_graph.py` | Main orchestration class |
| `tradingagents/graph/setup.py` | LangGraph workflow builder |
| `tradingagents/graph/conditional_logic.py` | Debate routing & termination |
| `tradingagents/agents/schemas.py` | Pydantic models for structured output |
| `tradingagents/default_config.py` | Config keys with `TRADINGAGENTS_*` env-var overrides |
| `tradingagents/llm_clients/factory.py` | LLM client factory |
| `tradingagents/llm_clients/capabilities.py` | Per-model capability table |
| `tradingagents/agents/utils/memory.py` | Decision log with Phase A/B reflection |
| `tradingagents/dataflows/interface.py` | Vendor routing (yfinance / Alpha Vantage) |
| `cli/main.py` | Interactive CLI with Rich live display |

### LLM Provider Architecture

All providers implement `BaseLLMClient` with a `get_llm()` method. Providers sharing the OpenAI-compatible API (`openai`, `xai`, `deepseek`, `qwen`, `glm`, `minimax`, `ollama`, `openrouter`) use `OpenAIClient` with provider-specific subclasses for quirks (DeepSeek reasoning_content roundtrip, MiniMax reasoning_split). Provider-specific clients exist for Anthropic, Google, and Azure.

### Data Vendor System

Tool calls route through `tradingagents/dataflows/interface.py` which dispatches to `yfinance` or `alpha_vantage` implementations based on `config["data_vendors"]`. Tool-level overrides in `config["tool_vendors"]` take precedence over category-level settings. Alpha Vantage rate limits trigger automatic fallback.

### Environment Configuration

API keys are read from environment variables (`.env`). The canonical mapping is in `tradingagents/llm_clients/api_key_env.py`. All config keys can be overridden via `TRADINGAGENTS_*` env vars (see `default_config.py._ENV_OVERRIDES`). Dual-region providers (qwen, glm, minimax) have separate international and China endpoints with distinct API keys.
