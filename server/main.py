import os
import re
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
import webhook_api as api

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Opik SDK client — traces sent via native SDK (UUIDv7 trace IDs available immediately)
# ---------------------------------------------------------------------------
_opik_client = None


def _init_opik():
    global _opik_client
    if not os.getenv("OPIK_API_KEY"):
        logger.info("OPIK_API_KEY not set — Opik tracing disabled")
        return
    try:
        import opik
        _opik_client = opik.Opik()  # reads OPIK_API_KEY / OPIK_WORKSPACE / OPIK_URL_OVERRIDE from env
        project = os.getenv("OPIK_PROJECT_NAME", "agentgateway-guardrails")
        logger.info(f"Opik SDK tracing enabled (project={project})")
    except Exception as exc:
        logger.warning(f"Could not init Opik SDK: {exc}")


# ---------------------------------------------------------------------------
# Local OTEL TracerProvider → fan-out-collector → Solo UI + Grafana Tempo
# Spans are children of the agentgateway W3C traceparent.
# ---------------------------------------------------------------------------
_local_tracer = None

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
_w3c_propagator = TraceContextTextMapPropagator()


def _init_local_otel():
    global _local_tracer
    endpoint = os.getenv("LOCAL_OTEL_ENDPOINT", "http://fan-out-collector.telemetry:4318")
    if not endpoint:
        return
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.semconv.resource import ResourceAttributes

        resource = Resource.create({ResourceAttributes.SERVICE_NAME: "guardrail-webhook"})
        exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        _local_tracer = provider.get_tracer("guardrail-webhook")

        logger.info(f"Local OTEL tracing enabled → {endpoint} (Solo UI / Grafana)")
    except Exception as exc:
        logger.warning(f"Could not initialise local OTEL: {exc}")


def _extract_agw_context(request: Request):
    """Extract OTEL parent context from agentgateway W3C traceparent header."""
    carrier = {}
    if tp := request.headers.get("traceparent"):
        carrier["traceparent"] = tp
    if ts := request.headers.get("tracestate"):
        carrier["tracestate"] = ts
    if not carrier:
        return None
    return _w3c_propagator.extract(carrier)


# ---------------------------------------------------------------------------
# Opik evaluation metrics (Sentiment + Tone)
# ---------------------------------------------------------------------------
_sentiment_metric = None
_tone_metric = None

TOXIC_PHRASES = [
    "you are stupid",
    "i hate you",
    "you are worthless",
    "kill yourself",
    "go die",
    "you are an idiot",
]

SENTIMENT_THRESHOLD = float(os.getenv("SENTIMENT_THRESHOLD", "-0.5"))


def _init_metrics():
    global _sentiment_metric, _tone_metric
    try:
        import nltk
        nltk.download("vader_lexicon", quiet=True)
    except Exception:
        pass
    try:
        from opik.evaluation.metrics import Sentiment, Tone
        _sentiment_metric = Sentiment()
        _tone_metric = Tone(forbidden_phrases=TOXIC_PHRASES)
        logger.info("Opik evaluation metrics initialised (Sentiment, Tone)")
    except Exception as exc:
        logger.warning(f"Opik metrics unavailable — falling back to pattern matching only: {exc}")


# ---------------------------------------------------------------------------
# PII regex patterns
# ---------------------------------------------------------------------------
PII_PATTERNS = {
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
}
MASK = "****"

BANNED_WORDS = [w.strip() for w in os.getenv("BANNED_WORDS", "violence,drugs,weapons,terrorism,exploit,abuse").split(",") if w.strip()]


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    _init_opik()
    _init_local_otel()
    _init_metrics()
    yield


app = FastAPI(
    title="Opik Guardrail Webhook",
    version="0.1.0",
    description=(
        "Guardrail webhook server powered by Opik for AgentGateway. "
        "Implements the Solo.io Guardrail Webhook API contract with "
        "PII detection, toxicity analysis, banned-word filtering, and "
        "Opik-based Sentiment / Tone evaluation."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Guardrail helpers
# ---------------------------------------------------------------------------
def _check_pii(text: str) -> dict[str, bool]:
    return {name: True for name, pat in PII_PATTERNS.items() if pat.search(text)}


def _mask_pii(text: str) -> str:
    for pat in PII_PATTERNS.values():
        text = pat.sub(MASK, text)
    return text


def _check_banned(text: str) -> str | None:
    lower = text.lower()
    return next((w for w in BANNED_WORDS if w in lower), None)


def _check_toxic(text: str) -> str | None:
    lower = text.lower()
    return next((p for p in TOXIC_PHRASES if p in lower), None)


def _check_sentiment(text: str) -> tuple[float, str | None]:
    if _sentiment_metric is None:
        return 0.0, None
    try:
        result = _sentiment_metric.score(output=text)
        return result.value, result.reason
    except Exception:
        return 0.0, None


def _check_tone(text: str) -> tuple[float, str | None]:
    if _tone_metric is None:
        return 1.0, None
    try:
        result = _tone_metric.score(output=text)
        return result.value, result.reason
    except Exception:
        return 1.0, None


# ---------------------------------------------------------------------------
# Shared state for request → response correlation
# ---------------------------------------------------------------------------
_pending_traces: dict[str, object] = {}  # agw_trace_id → opik Trace


def _extract_agentgateway_trace_id(request: Request) -> str | None:
    """Extract agentgateway trace ID from W3C traceparent header."""
    traceparent = request.headers.get("traceparent", "")
    if not traceparent:
        return None
    parts = traceparent.split("-")
    if len(parts) != 4:
        return None
    trace_id = parts[1]
    logger.info(f"🔗 traceparent received: agentgateway_trace_id={trace_id}")
    return trace_id


def _set_span_attrs(span, attrs: dict):
    """Set attributes on a local OTEL span, serialising non-scalar values to JSON."""
    for k, v in attrs.items():
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            span.set_attribute(k, json.dumps(v))
        else:
            span.set_attribute(k, v)


# ---------------------------------------------------------------------------
# POST /request — pre-hook guardrail
# ---------------------------------------------------------------------------
@app.post("/request", response_model=api.GuardrailsPromptResponse, tags=["Webhooks"])
async def process_prompts(
    request: Request,
    req: api.GuardrailsPromptRequest,
) -> api.GuardrailsPromptResponse:
    logger.info("📥 Incoming /request webhook")
    logger.info(f"traceparent: {request.headers.get('traceparent', '<not set>')}")

    agw_trace_id = _extract_agentgateway_trace_id(request)

    if _opik_client is None:
        return await _process_prompts_logic(req)

    # --- Opik SDK trace (UUIDv7 available immediately) ---
    project = os.getenv("OPIK_PROJECT_NAME", "agentgateway-guardrails")
    opik_trace = _opik_client.trace(
        name="guardrail-webhook",
        metadata={"agentgateway.trace_id": agw_trace_id or ""},
        project_name=project,
    )
    opik_trace_id = str(opik_trace.id)
    logger.info(f"📊 Opik trace created: {opik_trace_id}")

    # --- local OTEL span: child of agentgateway trace (Solo UI / Grafana) ---
    agw_ctx = _extract_agw_context(request)
    _last_agw_ctx = agw_ctx
    local_span = _local_tracer.start_span("guardrail-request", context=agw_ctx) if _local_tracer else None
    if local_span:
        local_span.set_attribute("opik.trace_id", opik_trace_id)
        if agw_trace_id:
            local_span.set_attribute("agentgateway.trace_id", agw_trace_id)

    # --- Opik request span ---
    req_span = opik_trace.span(
        name="guardrail-request",
        type="general",
        input={"messages": [{"role": m.role, "content": m.content} for m in req.body.messages]},
        metadata={"agentgateway.trace_id": agw_trace_id or ""},
    )

    def _finish_rejected(action: str, reason: str):
        req_span.end(output={"action": action, "reason": reason})
        opik_trace.end(output={"action": action})
        if local_span:
            _set_span_attrs(local_span, {"guardrail.action": action, "guardrail.reason": reason})

    try:
        should_mask = False

        for i, message in enumerate(req.body.messages):
            if not message.content:
                continue
            logger.info(f"→ Message[{i}] role={message.role}: {message.content}")
            content = message.content

            # --- toxic phrases ---
            s = req_span.span(name="toxic-phrases", input={"content": content})
            toxic = _check_toxic(content)
            s.end(output={"matched": toxic or ""})
            if toxic:
                logger.warning(f"⛔ RejectAction triggered: toxic phrase matched: '{toxic}'")
                _finish_rejected("reject", f"toxic phrase: {toxic}")
                return api.GuardrailsPromptResponse(
                    action=api.RejectAction(body=f"Rejected due to toxic language: matched phrase '{toxic}'", status_code=403, reason=f"toxic phrase: {toxic}"),
                )

            # --- banned words ---
            s = req_span.span(name="banned-words", input={"content": content})
            banned = _check_banned(content)
            s.end(output={"matched": banned or ""})
            if banned:
                logger.warning(f"⛔ RejectAction triggered: banned word matched: '{banned}'")
                _finish_rejected("reject", f"banned word: {banned}")
                return api.GuardrailsPromptResponse(
                    action=api.RejectAction(body=f"Rejected due to inappropriate content: matched word '{banned}'", status_code=403, reason=f"banned word: {banned}"),
                )

            # --- Opik sentiment ---
            s = req_span.span(name="opik-sentiment", input={"content": content, "threshold": SENTIMENT_THRESHOLD})
            compound, s_reason = _check_sentiment(content)
            s.end(output={"score": compound, "reason": s_reason or ""})
            if compound < SENTIMENT_THRESHOLD:
                logger.warning(f"⛔ RejectAction triggered: negative sentiment ({compound:.3f})")
                _finish_rejected("reject", f"sentiment: {compound:.3f}")
                return api.GuardrailsPromptResponse(
                    action=api.RejectAction(body="Rejected due to negative sentiment in content", status_code=403, reason=f"sentiment score: {compound:.3f}"),
                )

            # --- Opik tone ---
            s = req_span.span(name="opik-tone", input={"content": content})
            tone_score, t_reason = _check_tone(content)
            s.end(output={"score": tone_score, "reason": t_reason or ""})
            if tone_score < 0.5:
                logger.warning(f"⛔ RejectAction triggered: problematic tone ({tone_score:.3f}): {t_reason}")
                _finish_rejected("reject", f"tone: {t_reason}")
                return api.GuardrailsPromptResponse(
                    action=api.RejectAction(body=f"Rejected due to tone issues: {t_reason}", status_code=403, reason=f"tone score: {tone_score:.3f}"),
                )

            # --- PII detection & masking ---
            s = req_span.span(name="pii-detection", input={"content": content})
            pii = _check_pii(content)
            s.end(output={"matches": list(pii.keys())})
            if pii:
                for pii_type in pii:
                    logger.info(f"🔒 Matched PII pattern: {pii_type}")
                masked = _mask_pii(content)
                logger.info(f"🔒 Masking content: {content} → {masked}")
                req.body.messages[i].content = masked
                should_mask = True

        if should_mask:
            logger.info("✅ MaskAction returned (request)")
            req_span.end(output={"action": "mask", "reason": "PII detected and masked"})
            if local_span:
                _set_span_attrs(local_span, {"guardrail.action": "mask", "guardrail.reason": "PII detected and masked"})
            if agw_trace_id:
                _pending_traces[agw_trace_id] = opik_trace
            return api.GuardrailsPromptResponse(action=api.MaskAction(body=req.body, reason="PII detected and masked"))

        logger.info("✅ PassAction returned (request)")
        req_span.end(output={"action": "pass", "reason": "All checks passed"})
        if local_span:
            _set_span_attrs(local_span, {"guardrail.action": "pass", "guardrail.reason": "All checks passed"})
        if agw_trace_id:
            _pending_traces[agw_trace_id] = opik_trace
        return api.GuardrailsPromptResponse(action=api.PassAction(reason="All checks passed"))

    except Exception:
        req_span.end(output={"action": "error"})
        opik_trace.end(output={"action": "error"})
        raise
    finally:
        if local_span:
            local_span.end()


# ---------------------------------------------------------------------------
# POST /response — post-hook guardrail
# ---------------------------------------------------------------------------
@app.post("/response", response_model=api.GuardrailsResponseResponse, tags=["Webhooks"])
async def process_responses(
    request: Request,
    req: api.GuardrailsResponseRequest,
) -> api.GuardrailsResponseResponse:
    logger.info("📥 Incoming /response webhook")
    logger.info(f"traceparent: {request.headers.get('traceparent', '<not set>')}")

    agw_trace_id = _extract_agentgateway_trace_id(request)

    if _opik_client is None:
        return await _process_responses_logic(req)

    # Reuse the Opik trace opened in process_prompts (keyed by agw_trace_id)
    opik_trace = _pending_traces.pop(agw_trace_id, None) if agw_trace_id else None
    if opik_trace is None:
        project = os.getenv("OPIK_PROJECT_NAME", "agentgateway-guardrails")
        opik_trace = _opik_client.trace(
            name="guardrail-webhook",
            metadata={"agentgateway.trace_id": agw_trace_id or ""},
            project_name=project,
        )

    opik_trace_id = str(opik_trace.id)

    # local OTEL span: child of agentgateway trace (traceparent forwarded by agentgateway)
    agw_ctx = _extract_agw_context(request)
    local_span = _local_tracer.start_span("guardrail-response", context=agw_ctx) if _local_tracer else None
    if local_span:
        local_span.set_attribute("opik.trace_id", opik_trace_id)
        if agw_trace_id:
            local_span.set_attribute("agentgateway.trace_id", agw_trace_id)

    # Opik response span
    resp_span = opik_trace.span(
        name="guardrail-response",
        type="general",
        input={"choices": [{"role": c.message.role, "content": c.message.content} for c in req.body.choices]},
        metadata={"agentgateway.trace_id": agw_trace_id or ""},
    )

    try:
        should_mask = False

        for i, choice in enumerate(req.body.choices):
            if not choice.message.content:
                continue
            logger.info(f"→ Choice[{i}] role={choice.message.role}: {choice.message.content}")
            content = choice.message.content

            # --- PII masking ---
            s = resp_span.span(name="pii-detection", input={"content": content})
            pii = _check_pii(content)
            s.end(output={"matches": list(pii.keys())})
            if pii:
                for pii_type in pii:
                    logger.info(f"🔒 Matched PII in response: {pii_type}")
                masked = _mask_pii(content)
                logger.info(f"🔒 Masking response: {content} → {masked}")
                req.body.choices[i].message.content = masked
                should_mask = True

            # --- Opik sentiment ---
            s = resp_span.span(name="opik-sentiment", input={"content": content, "threshold": SENTIMENT_THRESHOLD})
            compound, s_reason = _check_sentiment(content)
            s.end(output={"score": compound, "reason": s_reason or ""})
            if compound < SENTIMENT_THRESHOLD:
                logger.warning(f"🔒 Masking response: negative sentiment ({compound:.3f})")
                req.body.choices[i].message.content = "[Content removed: safety policy violation]"
                should_mask = True

        if should_mask:
            logger.info("✅ MaskAction returned (response)")
            resp_span.end(output={"action": "mask", "reason": "Content masked in response"})
            opik_trace.end(output={"action": "mask"})
            if local_span:
                _set_span_attrs(local_span, {"guardrail.action": "mask", "guardrail.reason": "Content masked in response"})
            return api.GuardrailsResponseResponse(action=api.MaskAction(body=req.body, reason="Sensitive content masked"))

        logger.info("✅ PassAction returned (response)")
        resp_span.end(output={"action": "pass", "reason": "All checks passed"})
        opik_trace.end(output={"action": "pass"})
        if local_span:
            _set_span_attrs(local_span, {"guardrail.action": "pass", "guardrail.reason": "All checks passed"})
        return api.GuardrailsResponseResponse(action=api.PassAction(reason="All checks passed"))

    except Exception:
        resp_span.end(output={"action": "error"})
        opik_trace.end(output={"action": "error"})
        raise
    finally:
        if local_span:
            local_span.end()


# ---------------------------------------------------------------------------
# No-tracing fallback helpers (when _opik_client is None)
# ---------------------------------------------------------------------------
async def _process_prompts_logic(req: api.GuardrailsPromptRequest) -> api.GuardrailsPromptResponse:
    should_mask = False
    for i, message in enumerate(req.body.messages):
        if not message.content:
            continue
        content = message.content
        toxic = _check_toxic(content)
        if toxic:
            return api.GuardrailsPromptResponse(
                action=api.RejectAction(body=f"Rejected due to toxic language: matched phrase '{toxic}'", status_code=403, reason=f"toxic phrase: {toxic}"),
            )
        banned = _check_banned(content)
        if banned:
            return api.GuardrailsPromptResponse(
                action=api.RejectAction(body=f"Rejected due to inappropriate content: matched word '{banned}'", status_code=403, reason=f"banned word: {banned}"),
            )
        compound, _ = _check_sentiment(content)
        if compound < SENTIMENT_THRESHOLD:
            return api.GuardrailsPromptResponse(
                action=api.RejectAction(body="Rejected due to negative sentiment in content", status_code=403, reason=f"sentiment score: {compound:.3f}"),
            )
        tone_score, t_reason = _check_tone(content)
        if tone_score < 0.5:
            return api.GuardrailsPromptResponse(
                action=api.RejectAction(body=f"Rejected due to tone issues: {t_reason}", status_code=403, reason=f"tone score: {tone_score:.3f}"),
            )
        pii = _check_pii(content)
        if pii:
            req.body.messages[i].content = _mask_pii(content)
            should_mask = True
    if should_mask:
        return api.GuardrailsPromptResponse(action=api.MaskAction(body=req.body, reason="PII detected and masked"))
    return api.GuardrailsPromptResponse(action=api.PassAction(reason="All checks passed"))


async def _process_responses_logic(req: api.GuardrailsResponseRequest) -> api.GuardrailsResponseResponse:
    should_mask = False
    for i, choice in enumerate(req.body.choices):
        if not choice.message.content:
            continue
        content = choice.message.content
        pii = _check_pii(content)
        if pii:
            req.body.choices[i].message.content = _mask_pii(content)
            should_mask = True
        compound, _ = _check_sentiment(content)
        if compound < SENTIMENT_THRESHOLD:
            req.body.choices[i].message.content = "[Content removed: safety policy violation]"
            should_mask = True
    if should_mask:
        return api.GuardrailsResponseResponse(action=api.MaskAction(body=req.body, reason="Sensitive content masked"))
    return api.GuardrailsResponseResponse(action=api.PassAction(reason="All checks passed"))
