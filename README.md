# guardrail-webhook

[\![Release](https://github.com/day0ops/guardrail-webhook/actions/workflows/release.yml/badge.svg)](https://github.com/day0ops/guardrail-webhook/actions/workflows/release.yml)
[\![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[\![Image](https://img.shields.io/badge/registry-GAR-4285F4?logo=google-cloud)](https://console.cloud.google.com/artifacts/docker/field-engineering-apac/australia-southeast1/kasunt)

FastAPI webhook that enforces guardrail policies on agent traffic using [Opik](https://www.comet.com/site/products/opik/) for evaluation and tracing.

## What it does

Intercepts requests/responses passing through agentgateway and evaluates them against guardrail rules via Opik. Returns allow/deny decisions and traces results for observability.

## Usage

```bash
# Build image locally
make build IMAGE_TAG=latest

# Push to registry
make push IMAGE_REPO=australia-southeast1-docker.pkg.dev/field-engineering-apac/kasunt IMAGE_TAG=<tag>

# Deploy to Kubernetes (set OPIK_API_KEY to enable tracing)
OPIK_API_KEY=<key> make deploy IMAGE_REPO=australia-southeast1-docker.pkg.dev/field-engineering-apac/kasunt IMAGE_TAG=<tag>

# Tail logs
make logs
```

## Requirements

- Kubernetes cluster with agentgateway installed (`agentgateway-system` namespace)
- `kubectl` configured for the target cluster
- Opik API key (optional — tracing disabled if not set)
