IMAGE_REPO  ?=
IMAGE_PREFIX := $(if $(IMAGE_REPO),$(IMAGE_REPO)/,)
IMAGE_NAME  ?= opik-guardrail-webhook
IMAGE_TAG   ?= latest
NAMESPACE   ?= agentgateway-system
PLATFORMS   ?= linux/amd64,linux/arm64

.PHONY: build push deploy undeploy logs

build: ## Build multi-arch image (amd64 + arm64)
	docker buildx build --platform $(PLATFORMS) \
		--load -t $(IMAGE_PREFIX)$(IMAGE_NAME):$(IMAGE_TAG) server/

push: ## Build and push multi-arch image (amd64 + arm64)
	docker buildx build --platform $(PLATFORMS) \
		--push \
		-t $(IMAGE_PREFIX)$(IMAGE_NAME):$(IMAGE_TAG) \
		server/

deploy: ## Deploy the guardrail webhook to K8s
	kubectl apply -f config/serviceaccount.yaml
	kubectl apply -f config/service.yaml
	@if [ -n "$$OPIK_API_KEY" ]; then \
		kubectl create secret generic opik-secret \
			-n $(NAMESPACE) \
			--from-literal=api-key="$$OPIK_API_KEY" \
			--dry-run=client -o yaml | kubectl apply -f -; \
		echo "opik-secret created/updated"; \
	else \
		echo "OPIK_API_KEY not set — Opik tracing will be disabled"; \
	fi
	sed 's|image: opik-guardrail-webhook:latest|image: $(IMAGE_PREFIX)$(IMAGE_NAME):$(IMAGE_TAG)|' \
		config/deployment.yaml | kubectl apply -f -
	kubectl rollout status deployment/opik-guardrail-webhook -n $(NAMESPACE) --timeout=60s

undeploy: ## Remove the guardrail webhook from K8s
	kubectl delete -f config/deployment.yaml --ignore-not-found
	kubectl delete -f config/service.yaml --ignore-not-found
	kubectl delete -f config/serviceaccount.yaml --ignore-not-found
	kubectl delete secret opik-secret -n $(NAMESPACE) --ignore-not-found

logs: ## Tail the guardrail webhook logs
	kubectl logs -n $(NAMESPACE) deploy/opik-guardrail-webhook -f --tail=50
