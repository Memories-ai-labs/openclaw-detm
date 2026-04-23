"""Configuration for agentic-computer-use (DETM)."""
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()  # loads .env from project root (or cwd) if present

# Paths
DATA_DIR = Path(os.environ.get("ACU_DATA_DIR", Path.home() / ".agentic-computer-use"))
DB_PATH = DATA_DIR / "data.db"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"

# Display
DISPLAY = os.environ.get("DISPLAY", ":99")
DEFAULT_TASK_DISPLAY_WIDTH = int(os.environ.get("ACU_TASK_DISPLAY_WIDTH", "1280"))
DEFAULT_TASK_DISPLAY_HEIGHT = int(os.environ.get("ACU_TASK_DISPLAY_HEIGHT", "720"))

# Vision backend selection — default to openrouter (tested, no local GPU needed)
VISION_BACKEND = os.environ.get("ACU_VISION_BACKEND", "openrouter")  # openrouter|ollama|vllm|claude|passthrough

# Ollama
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
VISION_MODEL = os.environ.get("ACU_VISION_MODEL", "minicpm-v")
VISION_SYSTEM_INSTRUCTIONS = os.environ.get(
    "ACU_VISION_SYSTEM_INSTRUCTIONS",
    (
        "You are SmartWait, a visual condition evaluator for GUI/terminal screenshots. "
        "Look at the screenshot and decide if the stated condition is met. "
        "Be decisive: answer YES if the evidence is reasonably clear. "
        "Only answer NO if the evidence is genuinely absent or contradicts the condition. "
        "Follow the output format in the user prompt exactly."
    ),
)

# vLLM (for UI-TARS, Qwen, etc.)
VLLM_URL = os.environ.get("ACU_VLLM_URL", "http://localhost:8000")
VLLM_MODEL = os.environ.get("ACU_VLLM_MODEL", "ui-tars-1.5-7b")

# OpenRouter (cloud vision backend — Gemini Flash Lite, Claude Haiku, etc.)
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_VISION_MODEL = os.environ.get("ACU_OPENROUTER_VISION_MODEL", "google/gemini-2.0-flash-lite-001")

# UI-TARS grounding model via OpenRouter (used by gui_agent for precise cursor placement)
UITARS_OPENROUTER_MODEL = os.environ.get("ACU_UITARS_OPENROUTER_MODEL", "bytedance/ui-tars-1.5-7b")

# Qwen3-VL grounding model via OpenRouter
QWEN3VL_OPENROUTER_MODEL = os.environ.get("ACU_QWEN3VL_OPENROUTER_MODEL", "qwen/qwen3-vl-32b-instruct")

# Enable multi-view grounding (MVP). Increases grounding accuracy at cost of ~5x per move_to.
MVP_ENABLED = os.environ.get("ACU_MVP_ENABLED", "0") in ("1", "true", "yes")

# UI-TARS local via Ollama (used when no OpenRouter key)
UITARS_OLLAMA_MODEL = os.environ.get("ACU_UITARS_OLLAMA_MODEL", "0000/ui-tars-1.5-7b")
UITARS_KEEP_ALIVE = os.environ.get("ACU_UITARS_KEEP_ALIVE", "5m")

# Claude vision
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_VISION_MODEL = os.environ.get("ACU_CLAUDE_VISION_MODEL", "claude-sonnet-4-20250514")

# MAVI video intelligence API
MAVI_API_KEY = os.environ.get("MAVI_API_KEY", "")

# Live UI vision/control — backed by OpenRouter or Google AI Studio
# google/gemini-3-flash-preview — strong agentic/tool use. No caching via OpenRouter (3-preview
#   not on implicit list, and system prompt is below the 4096-token explicit-cache minimum),
#   but Google AI Studio direct offers native caching for this model.
OPENROUTER_LIVE_MODEL = os.environ.get("ACU_OPENROUTER_LIVE_MODEL", "google/gemini-3-flash-preview")

# Gemini backend for gui_agent supervisor. When set to "google_ai" (or if GEMINI_API_KEY is set
# and this is unset), direct Google AI Studio is used via OpenAI-compatible endpoint — bypasses
# OpenRouter routing overhead and enables native Gemini caching. Default "auto" picks AI Studio
# when GEMINI_API_KEY is present, otherwise falls back to OpenRouter.
GEMINI_BACKEND = os.environ.get("ACU_GEMINI_BACKEND", "auto")  # auto|openrouter|google_ai
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("ACU_GEMINI_API_KEY", "")

# EXPERIMENTAL: merge the supervisor's thinking + action into a single API call. The model
# populates a `thought` field in the tool arguments instead of emitting a separate thinking
# response. Cuts ~50% of per-turn LLM calls. May degrade action quality — tool-call selection
# happens with less deliberation. A/B test before flipping on in production.
MERGE_REASONING = os.environ.get("ACU_MERGE_REASONING", "false").lower() in ("true", "1", "yes")

# OpenClaw CLI path
OPENCLAW_CLI = os.environ.get("ACU_OPENCLAW_CLI", "openclaw")

# GUI Agent
GUI_AGENT_BACKEND = os.environ.get("ACU_GUI_AGENT_BACKEND", "direct")  # omniparser|uitars|claude_cu|direct

# OmniParser (SoM-based GUI grounding)
OMNIPARSER_PICKER_MODEL = os.environ.get("ACU_OMNIPARSER_PICKER_MODEL", "claude-haiku-4-5-20251001")
OMNIPARSER_BBOX_THRESHOLD = float(os.environ.get("ACU_OMNIPARSER_BBOX_THRESHOLD", "0.05"))
OMNIPARSER_IOU_THRESHOLD = float(os.environ.get("ACU_OMNIPARSER_IOU_THRESHOLD", "0.1"))

# Ollama model keepalive — "0" = unload immediately, "10m" = keep loaded for 10 min
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "10m")

# Smart Wait confidence / partial-streak thresholds
RESOLVE_CONFIDENCE_THRESHOLD = float(os.environ.get("ACU_RESOLVE_CONFIDENCE", "0.75"))
PARTIAL_STREAK_RESOLVE = int(os.environ.get("ACU_PARTIAL_STREAK_RESOLVE", "2"))

# OpenClaw
OPENCLAW_GATEWAY_PORT = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))

# Smart Wait defaults
DEFAULT_POLL_INTERVAL = 2.0  # seconds
MIN_POLL_INTERVAL = 0.5
MAX_POLL_INTERVAL = 5.0
DEFAULT_TIMEOUT = 300  # seconds
PIXEL_DIFF_THRESHOLD = 0.01  # 1% of pixels must change
DIFF_MAX_WIDTH = int(os.environ.get("ACU_DIFF_MAX_WIDTH", "320"))  # downsample before diff
MAX_STATIC_SECONDS = 30  # force vision re-eval even if diff gate says STATIC
STUCK_DETECTION_ENABLED = os.environ.get("ACU_STUCK_DETECTION", "0") in ("1", "true", "yes")
# Humanization — ON by default. Sub-agents/tools can flip via humanize_set MCP tool.
# Source of truth lives in src/agentic_computer_use/humanize.py; this mirror is for
# install.sh / configure section schemas to list it.
HUMANIZE_ENABLED_DEFAULT = os.environ.get("ACU_HUMANIZE", "1").strip().lower() in ("1", "true", "yes", "on")
FRAME_MAX_DIM = int(os.environ.get("ACU_FRAME_MAX_DIM", "960"))  # 960px is sufficient for YES/NO condition checks
FRAME_JPEG_QUALITY = int(os.environ.get("ACU_FRAME_JPEG_QUALITY", "72"))
# Grounding needs higher resolution than SmartWait — 1px error at 960px = 2px at 1920px
GROUNDING_MAX_DIM = int(os.environ.get("ACU_GROUNDING_MAX_DIM", "1920"))
GROUNDING_JPEG_QUALITY = int(os.environ.get("ACU_GROUNDING_JPEG_QUALITY", "80"))
# OpenClaw re-encodes images >1200px before forwarding to Claude, causing double-compression.
# Match that ceiling so images pass through unmodified at our chosen quality.
DESKTOP_LOOK_MAX_DIM = int(os.environ.get("ACU_DESKTOP_LOOK_DIM", "1200"))
DESKTOP_LOOK_JPEG_QUALITY = int(os.environ.get("ACU_DESKTOP_LOOK_QUALITY", "72"))
THUMBNAIL_MAX_DIM = 360
THUMBNAIL_JPEG_QUALITY = 60
MAX_CONTEXT_FRAMES = 4
MAX_CONTEXT_VERDICTS = 3

# Storage management
# Max MB for continuous frame recordings before oldest task-dirs are pruned (0 = unlimited)
MAX_RECORDINGS_MB = int(os.environ.get("ACU_MAX_RECORDINGS_MB", "1000"))
# Keep frame recordings after a task completes (cancelled/failed recordings are always deleted)
KEEP_RECORDINGS_ON_COMPLETE = os.environ.get("ACU_KEEP_RECORDINGS_ON_COMPLETE", "0") in ("1", "true", "yes")


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
