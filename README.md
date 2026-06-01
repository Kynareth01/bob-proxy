# bob-proxy

OpenAI-compatible API proxy for **IBM Bob Shell**. Wraps the `bob` CLI behind a standard `/v1/chat/completions` endpoint so any OpenAI SDK or tool (Hermes Agent, LangChain, Continue, etc.) can use IBM Bob as an LLM provider.

## How it works

```
Your app → POST /v1/chat/completions → bob-proxy → `bob -p` CLI → IBM Bob API
         ← OpenAI JSON response  ←              ← parsed output ←
```

Each request spawns a `bob` subprocess, parses its output, and returns a clean OpenAI-compatible JSON response.

## Quick Start

### Docker (recommended)

```bash
# Clone
git clone https://github.com/astraia9x/bob-proxy.git
cd bob-proxy

# Configure
cp .env.example .env
# Edit .env — at minimum set BOBSHELL_API_KEY

# Run
docker compose up -d
```

### Manual

```bash
# Prerequisites: Node.js ≥22, Python ≥3.10, Bob Shell installed
npm install -g bobshell

pip install -r requirements.txt

export BOBSHELL_API_KEY="your_key_here"
export PROXY_API_KEY="optional_proxy_password"

python server.py
```

## Usage

### curl

```bash
curl http://localhost:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your_proxy_api_key" \
  -d '{
    "model": "ibm-bob",
    "messages": [{"role": "user", "content": "Explain quicksort"}]
  }'
```

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8787/v1",
    api_key="your_proxy_api_key",
)

resp = client.chat.completions.create(
    model="ibm-bob",
    messages=[{"role": "user", "content": "Write a Python HTTP server"}],
)
print(resp.choices[0].message.content)
```

### Hermes Agent

```yaml
# ~/.hermes/config.yaml
custom_providers:
  bob:
    base_url: http://your-server:8787/v1
    api_key: your_proxy_api_key

model:
  provider: custom:bob
  default: ibm-bob
```

Or via CLI:
```bash
hermes config set model.provider custom:bob
hermes config set model.default ibm-bob
hermes config set model.base_url http://your-server:8787/v1
hermes config set model.api_key your_proxy_api_key
```

## Models

| Model ID | Bob Mode | Best for |
|---|---|---|
| `ibm-bob` | default | General tasks |
| `ibm-bob-code` | code | Code generation, refactoring |
| `ibm-bob-ask` | ask | Questions about codebases |
| `ibm-bob-plan` | plan | Architecture, planning |

## Configuration

| Env Var | Default | Description |
|---|---|---|
| `BOBSHELL_API_KEY` | required | IBM Bob API key (Inference type) |
| `PROXY_API_KEY` | (empty) | If set, clients must send this as Bearer token |
| `PORT` | `8787` | Server port |
| `HOST` | `0.0.0.0` | Bind address |
| `MAX_CONCURRENT` | `4` | Max concurrent bob processes |
| `BOB_TIMEOUT` | `120` | Per-request timeout (seconds) |
| `DEFAULT_MODEL` | `ibm-bob` | Default model ID |
| `BOB_BIN` | `bob` | Path to bob binary |

## Bobcoins Usage

Each request consumes Bobcoins from your IBM Bob Pro+ subscription:
- **Pro+**: 160 Bobcoins/month ($60/mo)
- Complex prompts (code generation, multi-file analysis) use more Bobcoins
- Use `MAX_CONCURRENT` to prevent runaway usage

## Streaming

Streaming is supported but simulated — Bob Shell doesn't expose token-by-token streaming to stdout, so the proxy runs the full command then chunks the output into SSE events. Compatible with any SSE client.

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check (no auth) |
| `/v1/models` | GET | List available models |
| `/v1/chat/completions` | POST | Chat completions (OpenAI-compatible) |

## License

MIT
