#!/usr/bin/env bash
# Start a vLLM OpenAI-compatible server for translation (data/nl2bash.py) and/or
# evaluation (evaluate.py's api_generate_fn), in its own venv via uv so its pinned
# torch/CUDA stack never collides with the training venv (AGENT.md section 6).
set -euo pipefail

MODEL="${1:-Qwen/Qwen3.5-4B}"
PORT="${2:-8000}"
API_KEY="${VLLM_API_KEY:-EMPTY}"

VENV_DIR="$(dirname "$0")/../.venv-vllm"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found; installing..." >&2
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.cargo/bin:$PATH"
fi

if [ ! -d "$VENV_DIR" ]; then
    uv venv "$VENV_DIR"
    # float16: matches T4 (no bf16 hardware); safe default even on newer GPUs.
    uv pip install --python "$VENV_DIR/bin/python" -U vllm
fi

exec "$VENV_DIR/bin/python" -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" \
    --port "$PORT" \
    --api-key "$API_KEY" \
    --dtype float16
