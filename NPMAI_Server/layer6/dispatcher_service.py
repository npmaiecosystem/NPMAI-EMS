"""
Layer 6 entrypoint - standalone process, same polling pattern as Layers
3-5 (polls layer5_outputs WHERE NOT dispatched, since Layer 5 doesn't
publish a new Redis event either - same documented trade-off carried
forward consistently).

Per output row: determine disposition (priority_mapping.py) -> check
dedup (dedup.py) -> if push-worthy, send to every active device token
registered for that building (app/fcm/client.py) -> record one
notification_history row per device (or one summary row if
IN_APP_ONLY/SUPPRESS with no device fanout) -> deactivate any token that
came back TOKEN_INVALID -> mark the source layer5_outputs row dispatched.
"""
import asyncio
import logging
import signal

from app.config import settings
from app.db import close_pool, get_pool, init_pool
from app.fcm.auth import FCMCredentials
from app.fcm.client import FCMClient, FCMSendOutcome
from app.layer6.dedup import is_severity_change
from app.layer6.priority_mapping import NotificationDisposition, disposition_for_severity

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 30.0


async def _dispatch_one_output(pool, fcm_client: FCMClient, row) -> None:
    disposition = disposition_for_severity(row["severity"])

    if disposition == NotificationDisposition.SUPPRESS:
        await pool.execute("UPDATE layer5_outputs SET dispatched = TRUE WHERE id = $1", row["id"])
        return

    should_push = disposition in (NotificationDisposition.PUSH_HIGH_PRIORITY, NotificationDisposition.PUSH_NORMAL)
    if should_push:
        changed = await is_severity_change(pool, row["panel_id"], row["breaker_id"], row["severity"])
        if not changed:
            # Downgrade to in-app-only for THIS cycle - still worth
            # logging (the app's audit history should show every cycle
            # ran), just not worth a push per requirement #4.
            disposition = NotificationDisposition.IN_APP_ONLY
            should_push = False

    if should_push:
        tokens = await pool.fetch(
            "SELECT fcm_token FROM device_tokens WHERE building_id = $1 AND is_active",
            row["building_id"],
        )
        if not tokens:
            logger.info("No active devices registered for building=%s - logging in-app only", row["building_id"])
            await _record_history(pool, row, disposition, fcm_token=None, push_sent=False, send_outcome=None)
        else:
            high_priority = disposition == NotificationDisposition.PUSH_HIGH_PRIORITY
            for t in tokens:
                await _send_and_record(pool, fcm_client, row, t["fcm_token"], high_priority)
    else:
        await _record_history(pool, row, disposition, fcm_token=None, push_sent=False, send_outcome=None)

    await pool.execute("UPDATE layer5_outputs SET dispatched = TRUE WHERE id = $1", row["id"])


async def _send_and_record(pool, fcm_client: FCMClient, row, fcm_token: str, high_priority: bool) -> None:
    data = {
        "layer5_output_id": row["id"], "severity": row["severity"],
        "panel_id": row["panel_id"] or "", "breaker_id": row["breaker_id"] or "",
    }
    result = await fcm_client.send(fcm_token, row["title"], row["explanation"], data, high_priority)

    if result.outcome == FCMSendOutcome.TOKEN_INVALID:
        logger.info("Deactivating invalid FCM token (first 12 chars: %s...)", fcm_token[:12])
        await pool.execute("UPDATE device_tokens SET is_active = FALSE WHERE fcm_token = $1", fcm_token)
    elif result.outcome in (FCMSendOutcome.TRANSIENT_ERROR, FCMSendOutcome.RATE_LIMITED):
        # Not retried within this cycle - the NEXT scheduled/event
        # output for this joint (or, worse case, the next audit cycle
        # a few hours later) will naturally re-attempt delivery, since
        # this row still gets marked dispatched either way (a stuck
        # "undispatched forever" row on a transient network blip is a
        # worse failure mode than occasionally missing one push during
        # a genuine FCM outage - see this file's module docstring on
        # why the polling model already tolerates this class of delay).
        logger.warning("FCM send failed (will not retry this cycle): %s", result.detail[:200])

    disposition = NotificationDisposition.PUSH_HIGH_PRIORITY if high_priority else NotificationDisposition.PUSH_NORMAL
    await _record_history(
        pool, row, disposition, fcm_token=fcm_token,
        push_sent=(result.outcome == FCMSendOutcome.SENT), send_outcome=result.outcome.value,
    )


async def _record_history(pool, row, disposition, fcm_token, push_sent, send_outcome) -> None:
    await pool.execute(
        """
        INSERT INTO notification_history
            (layer5_output_id, building_id, panel_id, breaker_id, severity, title, disposition, fcm_token, push_sent, send_outcome)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
        row["id"], row["building_id"], row["panel_id"], row["breaker_id"],
        row["severity"], row["title"], disposition.value, fcm_token, push_sent, send_outcome,
    )


async def _poll_once(pool, fcm_client: FCMClient) -> None:
    rows = await pool.fetch("SELECT * FROM layer5_outputs WHERE NOT dispatched ORDER BY ts ASC LIMIT 100")
    for row in rows:
        try:
            await _dispatch_one_output(pool, fcm_client, row)
        except Exception:
            logger.exception("Failed to dispatch layer5_outputs.id=%s - leaving undispatched, will retry next cycle", row["id"])


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("Starting Layer 6 notification dispatcher")

    pool = await init_pool()
    credentials = FCMCredentials(settings.fcm_service_account_json_path)
    fcm_client = FCMClient(credentials)

    stop_event = asyncio.Event()

    def _handle_stop(*_args):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop)
        except NotImplementedError:
            pass

    try:
        while not stop_event.is_set():
            try:
                await _poll_once(pool, fcm_client)
            except Exception:
                logger.exception("Poll cycle failed unexpectedly - continuing to next cycle")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        await close_pool()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
