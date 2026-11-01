"""Run LocationSpoof as a Raspberry Pi device: the web app plus the screen/button controller.

    python -m backend.pi [--driver auto|hat|sim] [--port 8765]

--driver sim runs the device as a web page at http://127.0.0.1:<port>/pi-sim (works on any computer).
The web app stays on 127.0.0.1 only: to use it from a laptop, tunnel over SSH
(ssh -L 8765:localhost:8765 pi@<pi-address>) rather than opening it to the network.
"""

import argparse
import asyncio
import logging

import uvicorn
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from backend import main as app_main
from backend.paths import WEB_DIR
from backend.pi import drivers
from backend.pi.controller import PiController

logger = logging.getLogger("locationspoof.pi")


class SimPress(BaseModel):
    button: str
    long: bool = False


def _add_simulator_routes(sim: drivers.SimDriver) -> None:
    app = app_main.app

    async def page():
        return FileResponse(WEB_DIR / "pi-sim.html")

    async def frame():
        if not sim.frame:
            raise HTTPException(503, "No frame yet")
        return Response(sim.frame, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def state():
        return {"frame_id": sim.frame_id, "led": sim.led, "beep": sim.last_beep, "beep_id": sim.beep_id}

    async def press(body: SimPress):
        if body.button not in ("A", "B", "X", "Y"):
            raise HTTPException(400, "Unknown button")
        sim.press(body.button, body.long)
        return {"ok": True}

    app.add_api_route("/pi-sim", page, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/pi-sim/frame.png", frame, methods=["GET"])
    app.add_api_route("/api/pi-sim/state", state, methods=["GET"])
    app.add_api_route("/api/pi-sim/button", press, methods=["POST"])


async def _run(args) -> None:
    driver = drivers.create(args.driver)
    if isinstance(driver, drivers.SimDriver):
        _add_simulator_routes(driver)
        logger.info(f"device simulator at http://127.0.0.1:{args.port}/pi-sim")

    controller = PiController(
        driver, app_main.devices, app_main.locations, app_main.places, app_main.player, app_main.alerts
    )
    config = uvicorn.Config(
        app_main.app, host="127.0.0.1", port=args.port, log_level="warning", loop="asyncio",
        timeout_graceful_shutdown=app_main.GRACEFUL_SHUTDOWN_S,
    )
    server = uvicorn.Server(config)
    app_main.app.state.uvicorn_server = server
    control = asyncio.create_task(controller.run(server), name="pi-controller")
    try:
        await server.serve()  # handles SIGTERM/Ctrl+C; its shutdown restores the iPhone's real location
    finally:
        control.cancel()
        try:
            await control
        except (asyncio.CancelledError, Exception):
            pass
        driver.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="LocationSpoof Raspberry Pi device")
    parser.add_argument("--driver", choices=["auto", "hat", "sim"], default="auto")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app_main._setup_file_log()
    app_main._raise_open_file_limit()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
