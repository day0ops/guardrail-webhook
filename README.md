# guardrail-webhook

[![Release](https://github.com/day0ops/guardrail-webhook/actions/workflows/release.yml/badge.svg)](https://github.com/day0ops/guardrail-webhook/actions/workflows/release.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Image](https://img.shields.io/badge/registry-GAR-4285F4?logo=googlecloud)](https://console.cloud.google.com/artifacts/docker/field-engineering-apac/australia-southeast1/kasunt)

FastAPI webhook that inspects and filters LLM traffic through [agentgateway](https://agentgateway.dev), integrated with [Opik](https://www.comet.com/docs/opik/) for trace observability.

## What it does

Receives webhook callbacks from agentgateway containing request/response messages, applies guardrail logic, and emits traces to Opik for monitoring and compliance.

## Usage

```bash
# Build image locally
make build IMAGE_TAG=latest

# Push to registry
make push IMAGE_REPO=australia-southeast1-docker.pkg.dev/field-engineering-apac/kasunt IMAGE_TAG=<tag>

# Deploy to Kubernetes
make deploy IMAGE_REPO=australia-southeast1-docker.pkg.dev/field-engineering-apac/kasunt IMAGE_TAG=<tag>

# Tail logs
make logs
```

## Requirements

- Kubernetes cluster with agentgateway installed (`agentgateway-system` namespace)
- `kubectl` configured for the target cluster
