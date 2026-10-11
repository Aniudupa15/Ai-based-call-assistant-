"""FastAPI app and start-up.

Run:  python -m app.main --config config.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import ApiError, build_router
from .calls import CallService
from .config import Config, load_config
from .db import Database
from .gateway import Gateway
from .notify import EventBus, Notifier
from .rules import RulesEngine
from .session import Engines
from .settings import Settings
from .tts import PhraseCache
from .vad import EnergyVad, make_vad

log = logging.getLogger("app")

TICK_S = 30
RETENTION_EVERY_S = 6 * 3600


def build_engines(config: Config, owner_name: str) -> Engines:
    m = config.models
    if m.backend == "fake":
        from .fakes import FakeLLM, FakeSTT, FakeTTS

        tts = FakeTTS()
        return Engines(stt=FakeSTT(), llm=FakeLLM(), tts=tts,
                       phrases=PhraseCache(tts, config.storage.phrase_dir, owner_name,
                                         languages=config.call.phrase_languages),
                       vad_factory=EnergyVad, loaded=["fake"])

    from .llm import OllamaLLM
    from .stt import IndicConformerSTT, RoutingSTT, WhisperSTT
    from .tts import IndicParlerTTS, MmsTTS, PiperTTS, RoutingTTS

    indic = any(l in config.call.languages for l in ("hi", "kn"))
    log.info("loading speech models on %s", m.device)
    indic_stt = None
    if indic and m.stt_indic:
        try:
            indic_stt = IndicConformerSTT(m.stt_indic, m.stt_indic_device,
                                          config.storage.model_dir)
        except Exception as e:  # gated model without a login, no network, ...
            log.warning("IndicConformer not loaded (%s); Whisper transcribes Hindi and "
                        "Kannada instead. See docs/HOME_SETUP.md to enable it.",
                        str(e).splitlines()[0][:200])
    stt = RoutingSTT(WhisperSTT(m.stt_english, m.device, m.stt_compute_type), indic_stt)
    indic_langs = [l for l in config.call.languages if l in ("hi", "kn")]
    if not indic:
        indic_tts = None
    elif m.tts_indic == "mms":
        indic_tts = MmsTTS(indic_langs, m.tts_indic_device)
    else:
        indic_tts = IndicParlerTTS(m.tts_indic, m.tts_indic_device)
    tts = RoutingTTS(PiperTTS(m.tts_english),
                     indic_tts)
    loaded = [m.stt_english, m.llm, m.tts_english]
    if indic:
        loaded += ([m.stt_indic] if indic_stt else []) + [m.tts_indic]
    return Engines(stt=stt, llm=OllamaLLM(m.ollama_url, m.llm), tts=tts,
                   phrases=PhraseCache(tts, config.storage.phrase_dir, owner_name,
                                         languages=config.call.phrase_languages),
                   vad_factory=lambda: make_vad(m.vad), loaded=loaded)


def create_app(config: Config, engines: Engines | None = None) -> FastAPI:
    db = Database(config.storage.database)
    settings = Settings(config, db)
    engines = engines or build_engines(config, settings.owner_name)
    bus = EventBus(db)
    notifier = Notifier(db, settings, config.notify, bus)
    rules = RulesEngine(db, settings, config.call.max_seconds)
    calls = CallService(db, settings, config, engines, rules, notifier)
    gateway = Gateway(config, settings, rules, calls, notifier, engines)
    state = SimpleNamespace(config=config, db=db, settings=settings, engines=engines, bus=bus,
                            notifier=notifier, rules=rules, calls=calls, gateway=gateway)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        calls.recover()
        tasks = [asyncio.create_task(_ticker(state)),
                 asyncio.create_task(_retention(state))]
        if config.models.backend != "fake":
            tasks.append(asyncio.create_task(_warm(state)))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(calls.wait_idle(), 30)
            aclose = getattr(engines.llm, "aclose", None)
            if aclose:
                with contextlib.suppress(Exception):
                    await aclose()

    app = FastAPI(title="Call assistant", version="1.0", lifespan=lifespan)
    app.state.s = state
    app.include_router(gateway.router())
    app.include_router(build_router(state))

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse({"error": exc.code, "detail": exc.detail}, exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse({"error": code, "detail": str(exc.detail)}, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", []))
        return JSONResponse({"error": "invalid", "detail": f"{where}: {first.get('msg', '')}"},
                            422)

    return app


async def _ticker(s) -> None:
    while True:
        await asyncio.sleep(TICK_S)
        try:
            await s.notifier.tick()
        except Exception:
            log.exception("notifier tick failed")


async def _retention(s) -> None:
    while True:
        try:
            s.calls.apply_retention()
        except Exception:
            log.exception("retention job failed")
        await asyncio.sleep(RETENTION_EVERY_S)


async def _warm(s) -> None:
    """Loads the language model and builds the phrase cache in the background."""
    warm = getattr(s.engines.llm, "warm", None)
    if warm:
        try:
            await warm()
        except Exception as e:
            log.warning("could not warm the language model: %s", e)
    try:
        await asyncio.to_thread(s.engines.phrases.build, s.config.call.languages)
    except Exception:
        log.exception("phrase cache build failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal AI call assistant server")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)

    import uvicorn

    # WebSocket ping every 5 s; three missed pings (15 s) mean the link is down.
    uvicorn.run(create_app(config), host=config.network.listen, port=config.network.port,
                ws_ping_interval=5, ws_ping_timeout=15, log_level=args.log_level)


if __name__ == "__main__":
    main()
