from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import config

log = logging.getLogger("server")


class Keepalive:
    """Pings our own public URL so Render doesn't put the service to sleep."""

    INTERVAL = 540  # 9 min (Render sleeps after ~15 min without inbound traffic)

    def __init__(self):
        self.until = 0.0
        self.last_ok: bool | None = None
        self.last_ts = 0.0
        self.busy = lambda: False
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(config.PUBLIC_URL)

    def keep_awake(self, hours: float):
        self.until = time.time() + hours * 3600

    def stop(self):
        self.until = 0.0

    def remaining(self) -> float:
        return max(0.0, self.until - time.time())

    async def ping_now(self) -> bool:
        if not self.enabled:
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(f"{config.PUBLIC_URL}/health") as r:
                    ok = r.status == 200
        except Exception as e:
            log.warning("self-ping failed: %s", e)
            ok = False
        self.last_ok = ok
        self.last_ts = time.time()
        return ok

    async def _loop(self):
        while True:
            await asyncio.sleep(self.INTERVAL)
            if self.enabled and (self.remaining() > 0 or self.busy()):
                await self.ping_now()

    def start(self):
        self._task = asyncio.create_task(self._loop())


keepalive = Keepalive()


def _health_app() -> web.Application:
    app = web.Application()

    async def health(_request):
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    return app


async def start_health_server():
    runner = web.AppRunner(_health_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    log.info("Health server listening on port %s", config.PORT)
    return runner


async def run_webhook(bot: Bot, dp: Dispatcher):
    app = _health_app()
    SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=config.WEBHOOK_SECRET
    ).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()

    await bot.set_webhook(
        url=f"{config.PUBLIC_URL}/webhook",
        secret_token=config.WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=False,
    )
    log.info("Webhook mode on %s (port %s)", config.PUBLIC_URL, config.PORT)
    await asyncio.Event().wait()  # run forever