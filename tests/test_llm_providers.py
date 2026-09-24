# -*- coding: utf-8 -*-
"""Tests for the three-tier LLM fallback chain (Gemini -> GLM -> OpenRouter).

Covers the operational contract agreed for the 429/503 incident:

* transient failures (503/timeout/bare 429) retry **in place** and must not
  downgrade (and therefore must never spend OpenRouter's tiny daily budget)
* quota / auth failures downgrade immediately
* OpenRouter is only reachable once the earlier tiers failed
* OpenRouter's daily ledger refuses calls at/after the limit and keeps a reserve
* the ledger persists across runs and mirrors into reports/latest.meta.json
* the JSON-output parameters are attached per provider and can be retried away
* the result cache round-trips and expires, tagging data_source=llm_cache
* provider precheck marks auth failures unhealthy and surfaces a
  ProviderUnavailableError translated to _AllModelsFailedError
"""
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Keep heavy/optional imports out of the way (same approach as sibling tests).
for _mod in ("litellm", "google.generativeai", "google.genai", "anthropic"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import pytest  # noqa: E402

from src.llm.provider_error import (  # noqa: E402
    LLMErrorType,
    ProviderUnavailableError,
    classify_llm_error,
)
from src.llm.provider_chain import (  # noqa: E402
    FallbackDecision,
    FallbackOutcome,
    RetryPolicy,
    build_fallback_policy,
    build_precheck_targets,
    decide_fallback,
    precheck_providers,
    resolve_provider_tier,
)
from src.llm.openrouter_quota import (  # noqa: E402
    OpenRouterQuotaLedger,
    create_openrouter_quota_ledger,
)
from src.llm.provider_rate_limit import create_provider_rate_limiter  # noqa: E402
from src.llm.llm_result_cache import (  # noqa: E402
    LLMResultCache,
    build_cache_key,
    create_llm_result_cache,
    hash_prompt,
)


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Provider exception double with a real HTTP status."""

    def __init__(self, message, *, status=None, headers=None):
        super().__init__(message)
        self.status_code = status
        if headers is not None:
            self.response = SimpleNamespace(headers=headers)


def _response(text="ok"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=None,
    )


GEMINI = "gemini/gemini-3.1-pro-preview"
GLM = "zhipuai/glm-4.6"
OPENROUTER = "openrouter/deepseek/deepseek-chat"


def _make_analyzer(tmp_path, *, dispatch, models=None, fallback=None, **overrides):
    """Build a GeminiAnalyzer wired to doubles, mirroring sibling test helpers."""
    cfg = MagicMock()
    cfg.litellm_model = GEMINI
    cfg.litellm_fallback_models = list(fallback if fallback is not None else [GLM, OPENROUTER])
    cfg.gemini_api_keys = ["sk-gemini-testkey-1234"]
    cfg.zhipu_api_keys = ["zhipu-testkey-1234"]
    cfg.openrouter_api_keys = ["sk-or-testkey-1234"]
    cfg.anthropic_api_keys = []
    cfg.openai_api_keys = []
    cfg.deepseek_api_keys = []
    cfg.llm_model_list = []
    cfg.llm_channels = []
    cfg.litellm_config_path = None
    cfg.openai_base_url = None
    cfg.generation_backend = "litellm"
    cfg.generation_fallback_backend = "litellm"
    # --- three-tier fallback knobs (primitives, never MagicMock) ---
    cfg.llm_provider_tiers = ["gemini", "zhipu", "openrouter"]
    cfg.llm_fallback_chain_enabled = True
    cfg.llm_fallback_max_in_place_retries = 2
    cfg.llm_fallback_backoff_base_seconds = 0.0
    cfg.llm_fallback_backoff_max_seconds = 0.0
    cfg.llm_json_output_mode = "off"
    cfg.openrouter_quota_guard_enabled = True
    cfg.openrouter_quota_daily_limit = 50
    cfg.openrouter_quota_reserve_after = 45
    cfg.openrouter_quota_per_run_cap = 0
    cfg.openrouter_quota_file = str(tmp_path / "openrouter_quota.json")
    cfg.openrouter_quota_meta_file = str(tmp_path / "latest.meta.json")
    cfg.llm_provider_rate_limit_enabled = False
    cfg.llm_provider_rpm_gemini = 10
    cfg.llm_provider_rpm_zhipu = 60
    cfg.llm_provider_rpm_openrouter = 5
    cfg.llm_provider_rpm_gha_halve = False
    cfg.llm_cache_enabled = False
    cfg.llm_cache_ttl_seconds = 86400
    cfg.llm_cache_path = str(tmp_path / "llm_cache.db")
    cfg.llm_provider_precheck_enabled = False
    cfg.llm_provider_precheck_timeout_seconds = 10.0
    for key, value in overrides.items():
        setattr(cfg, key, value)

    from src.analyzer import GeminiAnalyzer

    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    analyzer._router = None
    analyzer._litellm_available = True
    analyzer._legacy_router_model_list = []
    analyzer._config_override = cfg
    analyzer._dispatch_calls = []

    def _dispatch(self, model, call_kwargs, *, config=None, **kwargs):
        self._dispatch_calls.append({"model": model, "kwargs": dict(call_kwargs)})
        return dispatch(model)

    analyzer._dispatch_litellm_completion = _dispatch.__get__(analyzer, type(analyzer))
    return analyzer


def _models(analyzer):
    return [c["model"] for c in analyzer._dispatch_calls]


def _count(analyzer, model):
    return sum(1 for m in _models(analyzer) if m == model)


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------

class TestErrorClassification:
    def test_503_is_transient(self):
        assert classify_llm_error(ProviderError("503 Service Unavailable", status=503)) is LLMErrorType.TRANSIENT

    def test_timeout_is_transient(self):
        assert classify_llm_error(RuntimeError("request timeout after 30s")) is LLMErrorType.TRANSIENT

    def test_bare_429_is_transient(self):
        assert classify_llm_error(ProviderError("429 rate limit exceeded", status=429)) is LLMErrorType.TRANSIENT

    def test_429_with_quota_wording_is_quota_exhausted(self):
        err = ProviderError("429 You exceeded your current quota", status=429)
        assert classify_llm_error(err) is LLMErrorType.QUOTA_EXHAUSTED

    def test_insufficient_quota_is_quota_exhausted(self):
        assert classify_llm_error(RuntimeError("insufficient_quota")) is LLMErrorType.QUOTA_EXHAUSTED

    def test_402_is_quota_exhausted(self):
        assert classify_llm_error(ProviderError("payment required", status=402)) is LLMErrorType.QUOTA_EXHAUSTED

    def test_401_is_auth_error(self):
        assert classify_llm_error(ProviderError("invalid api key", status=401)) is LLMErrorType.AUTH_ERROR

    def test_403_is_auth_error(self):
        assert classify_llm_error(ProviderError("forbidden", status=403)) is LLMErrorType.AUTH_ERROR

    def test_unknown_error_defaults_to_transient(self):
        assert classify_llm_error(RuntimeError("something odd")) is LLMErrorType.TRANSIENT


class TestFallbackDecisionTable:
    def test_transient_retries_within_budget(self):
        outcome = decide_fallback(
            LLMErrorType.TRANSIENT, retry_policy=RetryPolicy(max_in_place_retries=2), attempt=1
        )
        assert outcome.decision is FallbackDecision.RETRY_IN_PLACE

    def test_transient_advances_once_budget_is_spent(self):
        outcome = decide_fallback(
            LLMErrorType.TRANSIENT, retry_policy=RetryPolicy(max_in_place_retries=2), attempt=3
        )
        assert outcome.decision is FallbackDecision.ADVANCE
        assert outcome.reason == "transient_retry_exhausted"

    def test_quota_advances_immediately(self):
        outcome = decide_fallback(
            LLMErrorType.QUOTA_EXHAUSTED, retry_policy=RetryPolicy(), attempt=1
        )
        assert outcome.decision is FallbackDecision.ADVANCE
        assert outcome.reason == "quota_exhausted"

    def test_auth_advances_and_marks_unhealthy(self):
        outcome = decide_fallback(LLMErrorType.AUTH_ERROR, retry_policy=RetryPolicy(), attempt=1)
        assert outcome.decision is FallbackDecision.ADVANCE
        assert outcome.mark_unhealthy is True

    def test_invalid_request_aborts(self):
        outcome = decide_fallback(
            LLMErrorType.INVALID_REQUEST, retry_policy=RetryPolicy(), attempt=1
        )
        assert outcome.decision is FallbackDecision.ABORT

    def test_json_mode_rejection_retries_without_json(self):
        outcome = decide_fallback(
            LLMErrorType.INVALID_REQUEST,
            retry_policy=RetryPolicy(),
            attempt=1,
            error=ProviderError("Invalid request: response_format is not supported", status=400),
            json_mode_active=True,
        )
        assert outcome.decision is FallbackDecision.RETRY_WITHOUT_JSON_MODE


# ---------------------------------------------------------------------------
# in-place retry vs downgrade (analyzer integration)
# ---------------------------------------------------------------------------

class TestGeminiRetryAndDowngrade:
    def test_single_503_retries_in_place_and_never_calls_openrouter(self, tmp_path):
        seen = {"gemini": 0}

        def dispatch(model):
            if model == GEMINI:
                seen["gemini"] += 1
                if seen["gemini"] == 1:
                    raise ProviderError("503 Service Unavailable", status=503)
                return _response('{"ok": true}')
            raise AssertionError("unexpected downgrade to %s" % model)

        analyzer = _make_analyzer(tmp_path, dispatch=dispatch)
        text, model, _usage = analyzer._call_litellm_impl("prompt", {"temperature": 0.0})

        assert text == '{"ok": true}'
        assert model == GEMINI
        assert _count(analyzer, GEMINI) == 2, "503 must be retried in place"
        assert _count(analyzer, GLM) == 0
        assert _count(analyzer, OPENROUTER) == 0, "a single 503 must never spend OpenRouter"

    def test_503_retries_exhausted_then_downgrades_to_glm(self, tmp_path):
        def dispatch(model):
            if model == GEMINI:
                raise ProviderError("503 Service Unavailable", status=503)
            if model == GLM:
                return _response('{"ok": "glm"}')
            raise AssertionError("unexpected model %s" % model)

        analyzer = _make_analyzer(tmp_path, dispatch=dispatch)
        text, model, _usage = analyzer._call_litellm_impl("prompt", {"temperature": 0.0})

        assert model == GLM
        assert _count(analyzer, GEMINI) == 3, "1 initial + 2 in-place retries"
        assert _count(analyzer, GLM) == 1
        assert _count(analyzer, OPENROUTER) == 0

    def test_gemini_quota_exhausted_downgrades_to_glm_immediately(self, tmp_path):
        def dispatch(model):
            if model == GEMINI:
                raise ProviderError(
                    "429 You exceeded your current quota, please check your plan", status=429
                )
            if model == GLM:
                return _response('{"ok": "glm"}')
            raise AssertionError("unexpected model %s" % model)

        analyzer = _make_analyzer(tmp_path, dispatch=dispatch)
        text, model, _usage = analyzer._call_litellm_impl("prompt", {"temperature": 0.0})

        assert model == GLM
        assert _count(analyzer, GEMINI) == 1, "quota exhaustion must not be retried in place"
        assert _count(analyzer, OPENROUTER) == 0

    def test_both_tiers_quota_exhausted_triggers_openrouter(self, tmp_path):
        def dispatch(model):
            if model in (GEMINI, GLM):
                raise ProviderError("quota exceeded for this key", status=429)
            if model == OPENROUTER:
                return _response('{"ok": "openrouter"}')
            raise AssertionError("unexpected model %s" % model)

        analyzer = _make_analyzer(tmp_path, dispatch=dispatch)
        text, model, _usage = analyzer._call_litellm_impl("prompt", {"temperature": 0.0})

        assert model == OPENROUTER
        assert _count(analyzer, OPENROUTER) == 1
        ledger = analyzer._get_openrouter_quota_ledger()
        assert ledger.count == 1, "a successful OpenRouter call must be charged"

    def test_auth_error_marks_provider_unhealthy_and_is_skipped_afterwards(self, tmp_path):
        def dispatch(model):
            if model == GEMINI:
                raise ProviderError("invalid api key", status=401)
            if model == GLM:
                return _response('{"ok": "glm"}')
            raise AssertionError("unexpected model %s" % model)

        analyzer = _make_analyzer(tmp_path, dispatch=dispatch)
        analyzer._call_litellm_impl("prompt", {"temperature": 0.0})

        policy = analyzer._get_fallback_policy()
        assert policy.health.is_healthy("gemini") is False
        assert policy.skip_reason_for("gemini"), "unhealthy tier must be skipped next time"

        analyzer._dispatch_calls.clear()
        analyzer._call_litellm_impl("prompt", {"temperature": 0.0})
        assert _count(analyzer, GEMINI) == 0, "gemini must be skipped on the next call"


# ---------------------------------------------------------------------------
# OpenRouter quota protection
# ---------------------------------------------------------------------------

class TestOpenRouterQuotaProtection:
    def test_openrouter_refused_when_daily_limit_reached(self, tmp_path):
        ledger_path = tmp_path / "openrouter_quota.json"
        ledger_path.write_text(
            json.dumps({"date": _today(), "count": 50}), encoding="utf-8"
        )

        def dispatch(model):
            if model in (GEMINI, GLM):
                raise ProviderError("quota exceeded", status=429)
            raise AssertionError("OpenRouter must not be called at the daily limit")

        analyzer = _make_analyzer(tmp_path, dispatch=dispatch)
        with pytest.raises(Exception) as excinfo:
            analyzer._call_litellm_impl("prompt", {"temperature": 0.0})

        assert _count(analyzer, OPENROUTER) == 0
        assert "All LLM models failed" in str(excinfo.value)

    def test_openrouter_refused_when_quota_file_missing_but_env_limit_zero(self, tmp_path):
        def dispatch(model):
            if model in (GEMINI, GLM):
                raise ProviderError("quota exceeded", status=429)
            raise AssertionError("OpenRouter must not be called")

        analyzer = _make_analyzer(tmp_path, dispatch=dispatch, openrouter_quota_daily_limit=0)
        with pytest.raises(Exception):
            analyzer._call_litellm_impl("prompt", {"temperature": 0.0})
        assert _count(analyzer, OPENROUTER) == 0

    def test_ledger_reserve_only_allows_quota_reasons(self, tmp_path):
        ledger = OpenRouterQuotaLedger(
            path=tmp_path / "q.json",
            meta_path=tmp_path / "latest.meta.json",
            daily_limit=50,
            reserve_after=45,
            per_run_cap=100,
        ).load()
        ledger.record(46)

        assert ledger.allow(LLMErrorType.TRANSIENT).allowed is False
        assert ledger.allow(LLMErrorType.QUOTA_EXHAUSTED).allowed is True

    def test_ledger_refuses_at_hard_limit_for_any_reason(self, tmp_path):
        ledger = OpenRouterQuotaLedger(
            path=tmp_path / "q.json",
            meta_path=tmp_path / "latest.meta.json",
            daily_limit=50,
            reserve_after=45,
            per_run_cap=100,
        ).load()
        ledger.record(50)

        assert ledger.allow(LLMErrorType.QUOTA_EXHAUSTED).allowed is False

    def test_ledger_persists_and_mirrors_into_latest_meta(self, tmp_path):
        sidecar = tmp_path / "openrouter_quota.json"
        meta = tmp_path / "latest.meta.json"
        ledger = OpenRouterQuotaLedger(path=sidecar, meta_path=meta, daily_limit=50).load()
        ledger.record(3)
        ledger.record(1)

        assert sidecar.exists(), "quota sidecar must be written"
        reloaded = OpenRouterQuotaLedger(path=sidecar, meta_path=meta, daily_limit=50).load()
        assert reloaded.count == 4, "quota counter must survive a reload"

        payload = json.loads(meta.read_text(encoding="utf-8"))
        usage = payload.get("openrouter_usage")
        assert isinstance(usage, dict), "latest.meta.json must carry an openrouter_usage mirror"
        assert usage.get("count") == 4

    def test_ledger_reads_back_from_meta_mirror_when_sidecar_is_lost(self, tmp_path):
        sidecar = tmp_path / "openrouter_quota.json"
        meta = tmp_path / "latest.meta.json"
        ledger = OpenRouterQuotaLedger(path=sidecar, meta_path=meta, daily_limit=50).load()
        ledger.record(7)
        sidecar.unlink()

        recovered = OpenRouterQuotaLedger(path=sidecar, meta_path=meta, daily_limit=50).load()
        assert recovered.count == 7

    def test_ledger_resets_on_a_new_utc_day(self, tmp_path):
        sidecar = tmp_path / "openrouter_quota.json"
        sidecar.write_text(json.dumps({"date": "1999-01-01", "count": 42}), encoding="utf-8")
        ledger = OpenRouterQuotaLedger(path=sidecar, meta_path=None, daily_limit=50).load()
        assert ledger.count == 0

    def test_factory_uses_configured_paths(self, tmp_path):
        cfg = SimpleNamespace(
            openrouter_quota_file=str(tmp_path / "custom.json"),
            openrouter_quota_meta_file=str(tmp_path / "custom.meta.json"),
            openrouter_quota_daily_limit=12,
            openrouter_quota_reserve_after=9,
            openrouter_quota_per_run_cap=4,
        )
        ledger = create_openrouter_quota_ledger(cfg)
        ledger.record(1)
        assert (tmp_path / "custom.json").exists()
        assert (tmp_path / "custom.meta.json").exists()
        assert ledger.limit == 12
        assert ledger.reserve_after == 9


# ---------------------------------------------------------------------------
# token bucket / rate limiting
# ---------------------------------------------------------------------------

class TestProviderRateLimit:
    def test_limiter_is_disabled_when_configured_off(self, tmp_path):
        cfg = SimpleNamespace(llm_provider_rate_limit_enabled=False)
        limiter = create_provider_rate_limiter(cfg)
        assert limiter.enabled is False
        assert limiter.acquire("gemini").allowed is True
        assert limiter.acquire("gemini").allowed is True

    def test_gemini_rpm_is_halved_under_github_actions(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        cfg = SimpleNamespace(
            llm_provider_rate_limit_enabled=True,
            llm_provider_rpm_gemini=10,
            llm_provider_rpm_zhipu=60,
            llm_provider_rpm_openrouter=5,
            llm_provider_rpm_gha_halve=True,
        )
        limiter = create_provider_rate_limiter(cfg)
        assert limiter.rate_for("gemini") == pytest.approx(5.0)
        assert limiter.rate_for("zhipu") == pytest.approx(60.0), "only Gemini is halved"

    def test_rate_for_unknown_provider_is_zero(self, tmp_path):
        cfg = SimpleNamespace(
            llm_provider_rate_limit_enabled=True,
            llm_provider_rpm_gemini=10,
            llm_provider_rpm_zhipu=60,
            llm_provider_rpm_openrouter=5,
            llm_provider_rpm_gha_halve=False,
        )
        limiter = create_provider_rate_limiter(cfg)
        assert limiter.rate_for("openrouter") == pytest.approx(5.0)
        assert limiter.rate_for("some-other") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# JSON output enforcement
# ---------------------------------------------------------------------------

class TestJsonOutput:
    def test_gemini_gets_native_json_parameters(self, tmp_path):
        cfg = SimpleNamespace(llm_json_output_mode="on")
        from src.llm.provider_chain import apply_json_output_kwargs

        kwargs = {}
        applied = apply_json_output_kwargs(
            kwargs, GEMINI, mode="on", json_expected=False
        )
        assert applied is True
        assert kwargs["response_format"] == {"type": "json_object"}
        assert kwargs["extra_body"]["response_mime_type"] == "application/json"
        del cfg

    def test_zhipu_and_openrouter_get_response_format_only(self):
        from src.llm.provider_chain import apply_json_output_kwargs

        for model in (GLM, OPENROUTER):
            kwargs = {}
            apply_json_output_kwargs(kwargs, model, mode="on", json_expected=False)
            assert kwargs["response_format"] == {"type": "json_object"}
            assert "response_mime_type" not in json.dumps(kwargs.get("extra_body", {}))

    def test_auto_mode_skips_calls_without_json_expectation(self):
        from src.llm.provider_chain import apply_json_output_kwargs

        kwargs = {}
        applied = apply_json_output_kwargs(
            kwargs, GEMINI, mode="auto", json_expected=False
        )
        assert applied is False
        assert kwargs == {}

    def test_off_mode_never_attaches(self):
        from src.llm.provider_chain import apply_json_output_kwargs

        kwargs = {}
        assert apply_json_output_kwargs(kwargs, GEMINI, mode="off", json_expected=True) is False
        assert kwargs == {}

    def test_strip_removes_only_json_hints(self):
        from src.llm.provider_chain import strip_json_output_kwargs

        kwargs = {
            "response_format": {"type": "json_object"},
            "extra_body": {"response_mime_type": "application/json", "thinking": {"type": "x"}},
        }
        strip_json_output_kwargs(kwargs)
        assert "response_format" not in kwargs
        assert kwargs["extra_body"] == {"thinking": {"type": "x"}}


# ---------------------------------------------------------------------------
# LLM result cache
# ---------------------------------------------------------------------------

class TestLlmResultCache:
    def test_round_trip_and_data_source_tag(self, tmp_path):
        cache = LLMResultCache(tmp_path / "c.db", ttl_seconds=3600)
        ph = hash_prompt("prompt body", "system body")
        assert cache.put("600519", "2026-09-24", ph, '{"a":1}', model=GEMINI) is True

        hit = cache.get("600519", "2026-09-24", ph)
        assert hit is not None
        assert hit.text == '{"a":1}'
        assert hit.data_source == "llm_cache"
        assert hit.model == GEMINI

    def test_different_trade_date_is_a_miss(self, tmp_path):
        cache = LLMResultCache(tmp_path / "c.db", ttl_seconds=3600)
        ph = hash_prompt("p")
        cache.put("600519", "2026-09-24", ph, "x")
        assert cache.get("600519", "2026-09-25", ph) is None

    def test_different_stock_code_is_a_miss(self, tmp_path):
        cache = LLMResultCache(tmp_path / "c.db", ttl_seconds=3600)
        ph = hash_prompt("p")
        cache.put("600519", "2026-09-24", ph, "x")
        assert cache.get("000001", "2026-09-24", ph) is None

    def test_expired_entry_is_a_miss(self, tmp_path):
        clock = {"now": 1000.0}
        cache = LLMResultCache(tmp_path / "c.db", ttl_seconds=60, clock=lambda: clock["now"])
        ph = hash_prompt("p")
        cache.put("600519", "2026-09-24", ph, "x")
        clock["now"] += 61
        assert cache.get("600519", "2026-09-24", ph) is None

    def test_key_is_sha256_of_components(self, tmp_path):
        key = build_cache_key("600519", "2026-09-24", "abc")
        assert len(key) == 64
        assert key == build_cache_key("600519", "2026-09-24", "abc")
        assert key != build_cache_key("600519", "2026-09-24", "abd")

    def test_disabled_cache_is_a_noop(self, tmp_path):
        cache = LLMResultCache(tmp_path / "c.db", enabled=False)
        assert cache.enabled is False
        assert cache.get("a", "b", "c") is None
        assert cache.put("a", "b", "c", "d") is False

    def test_factory_respects_config_path_and_ttl(self, tmp_path):
        cfg = SimpleNamespace(
            llm_cache_enabled=True,
            llm_cache_path=str(tmp_path / "cfg.db"),
            llm_cache_ttl_seconds=120,
        )
        cache = create_llm_result_cache(cfg)
        assert cache.enabled is True
        assert cache.path == tmp_path / "cfg.db"
        assert cache._ttl == 120


# ---------------------------------------------------------------------------
# provider precheck
# ---------------------------------------------------------------------------

class TestProviderPrecheck:
    def test_healthy_path_records_each_tier(self):
        health_calls = []

        class _Health:
            def set_health(self, provider, healthy, **kwargs):
                health_calls.append((provider, healthy))

            def mark_healthy(self, provider, **kwargs):
                health_calls.append((provider, True))

        report = precheck_providers(
            [("gemini", GEMINI), ("zhipu", GLM)],
            ping=lambda model, timeout: {"ok": True},
            health=_Health(),
        )
        # display names: gemini / glm
        assert report.healthy_providers == ["gemini", "glm"]
        assert health_calls == [("gemini", True), ("zhipu", True)]

    def test_auth_failure_marks_unhealthy(self):
        registry = MagicMock()

        def ping(model, timeout):
            if model == GEMINI:
                raise ProviderError("invalid api key", status=401)
            return {"ok": True}

        report = precheck_providers(
            [("gemini", GEMINI), ("zhipu", GLM)],
            ping=ping,
            health=registry,
            strict=True,
        )
        assert report.unhealthy_providers == ["gemini"]
        assert "auth_error" in report.results[0].reason
        assert registry.set_health.called

    def test_transient_failure_does_not_kill_a_provider(self):
        def ping(model, timeout):
            raise ProviderError("503 Service Unavailable", status=503)

        report = precheck_providers([("gemini", GEMINI)], ping=ping)
        assert report.healthy_providers == ["gemini"], "a blip must not disable the tier"

    def test_all_providers_unhealthy_raises(self):
        def ping(model, timeout):
            raise ProviderError("invalid api key", status=401)

        with pytest.raises(ProviderUnavailableError):
            precheck_providers(
                [("gemini", GEMINI), ("zhipu", GLM)],
                ping=ping,
                strict=True,
            )

    def test_precheck_charges_openrouter_quota(self, tmp_path):
        ledger = OpenRouterQuotaLedger(
            path=tmp_path / "q.json", meta_path=None, daily_limit=50
        ).load()
        precheck_providers(
            [("openrouter", OPENROUTER)],
            ping=lambda model, timeout: {"ok": True},
            quota_ledger=ledger,
        )
        assert ledger.count == 1, "an OpenRouter precheck is a real call and must be charged"

    def test_precheck_skipped_when_quota_is_exhausted(self, tmp_path):
        ledger = OpenRouterQuotaLedger(
            path=tmp_path / "q.json", meta_path=None, daily_limit=50
        ).load()
        ledger.record(50)
        called = {"n": 0}

        def ping(model, timeout):
            called["n"] += 1
            return {"ok": True}

        report = precheck_providers(
            [("openrouter", OPENROUTER)], ping=ping, quota_ledger=ledger
        )
        assert called["n"] == 0
        assert report.skipped, "exhausted quota must skip the precheck"

    def test_build_precheck_targets_keeps_fallback_order(self):
        targets = build_precheck_targets([GLM, GEMINI, OPENROUTER, GLM])
        assert targets == [("gemini", GEMINI), ("zhipu", GLM), ("openrouter", OPENROUTER)]

    def test_analyzer_translates_provider_unavailable_into_all_models_failed(self, tmp_path):
        def dispatch(model):
            raise ProviderError("invalid api key", status=401)

        analyzer = _make_analyzer(
            tmp_path, dispatch=dispatch, llm_provider_precheck_enabled=True
        )

        from src.analyzer import _AllModelsFailedError

        # The precheck is skipped under pytest by design (mocks cannot model
        # provider auth); drop the guard just for this call.
        saved = sys.modules.pop("pytest", None)
        try:
            with pytest.raises(_AllModelsFailedError):
                analyzer.ensure_provider_precheck()
        finally:
            if saved is not None:
                sys.modules["pytest"] = saved


# ---------------------------------------------------------------------------
# tier resolution
# ---------------------------------------------------------------------------

class TestTierResolution:
    def test_openrouter_is_matched_before_generic_prefixes(self):
        assert resolve_provider_tier("openrouter/anthropic/claude-sonnet-4") == "openrouter"

    def test_gemini_variants(self):
        assert resolve_provider_tier("gemini/gemini-3.1-pro-preview") == "gemini"
        assert resolve_provider_tier("models/gemini-2.5-flash") == "gemini"

    def test_zhipu_variants(self):
        assert resolve_provider_tier("zhipuai/glm-4.6") == "zhipu"
        assert resolve_provider_tier("zhipu/glm-4-plus") == "zhipu"

    def test_unknown_provider(self):
        assert resolve_provider_tier("deepseek/deepseek-chat") == "other"

    def test_policy_orders_tiers(self):
        policy = build_fallback_policy(None)
        assert policy.tier_rank("gemini") < policy.tier_rank("zhipu") < policy.tier_rank("openrouter")
        assert policy.next_tier("gemini") == "zhipu"
        assert policy.next_tier("openrouter") is None


# ---------------------------------------------------------------------------
# analyzer-level cache integration
# ---------------------------------------------------------------------------

class TestAnalyzerLlmCacheIntegration:
    """The cache must remove the *second* identical LLM call, not the first."""

    @staticmethod
    def _analyzer(tmp_path):
        return _make_analyzer(
            tmp_path,
            dispatch=lambda model: _response("first response"),
            llm_cache_enabled=True,
            llm_cache_path=str(tmp_path / "llm_cache.db"),
            llm_cache_ttl_seconds=86400,
            llm_provider_precheck_enabled=False,
            report_integrity_enabled=False,
            report_integrity_retry=0,
            report_language="zh",
            gemini_request_delay=0,
        )

    @staticmethod
    def _context():
        return {
            "code": "600519",
            "stock_name": "贵州茅台",
            "trade_date": "2026-09-24",
        }

    def _run(self, analyzer, *, result, calls, persisted):
        from src.analyzer import AnalysisResult

        def _call_litellm(*args, **kwargs):
            calls.append(1)
            return (
                "first response",
                GEMINI,
                {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            )

        def _persist(usage, model, call_type, stock_code=None):
            persisted.append(dict(usage or {}))

        with patch.object(analyzer, "is_available", return_value=True), \
             patch.object(analyzer, "_get_analysis_system_prompt", return_value="system"), \
             patch.object(analyzer, "_format_prompt", return_value="prompt"), \
             patch.object(analyzer, "_call_litellm", side_effect=_call_litellm), \
             patch.object(analyzer, "_parse_response", return_value=result), \
             patch.object(analyzer, "_build_market_snapshot", return_value={}), \
             patch.object(analyzer, "_check_content_integrity", return_value=(True, [])), \
             patch("src.analyzer.persist_llm_usage", side_effect=_persist):
            return analyzer.analyze(self._context())

    def test_second_analyze_is_served_from_cache(self, tmp_path):
        analyzer = self._analyzer(tmp_path)
        from src.analyzer import AnalysisResult

        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            sentiment_score=80,
            trend_prediction="看多",
            operation_advice="持有",
            analysis_summary="首次结果",
        )
        calls = []
        persisted = []

        first = self._run(analyzer, result=result, calls=calls, persisted=persisted)
        second = self._run(analyzer, result=result, calls=calls, persisted=persisted)

        # 两次 analyze 只应真实调用一次 LLM；第二次由缓存承接。
        assert len(calls) == 1
        assert first.analysis_summary == "首次结果"
        assert second.analysis_summary == "首次结果"
        # 缓存命中必须打上 data_source=llm_cache 标记，便于观测与计费统计。
        assert persisted[1].get("data_source") == "llm_cache"
        assert persisted[0].get("data_source") != "llm_cache"

    def test_failed_second_attempt_falls_through_to_llm_again(self, tmp_path):
        """A cache miss on a different trade date must still reach the provider."""
        analyzer = self._analyzer(tmp_path)
        from src.analyzer import AnalysisResult

        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            sentiment_score=80,
            trend_prediction="看多",
            operation_advice="持有",
            analysis_summary="首次结果",
        )
        calls = []
        persisted = []

        self._run(analyzer, result=result, calls=calls, persisted=persisted)
        # 同一分析器、不同交易日 → 缓存 key 不同 → 必须重新调用。
        with patch.object(analyzer, "_resolve_llm_cache_trade_date", return_value="2026-09-25"):
            self._run(analyzer, result=result, calls=calls, persisted=persisted)

        assert len(calls) == 2


def _today():
    from src.llm.openrouter_quota import _utc_date

    return _utc_date()
