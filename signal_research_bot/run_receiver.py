"""Entrypoint for the long-running receiver container.

Does one thing: connect to signal-cli and write what arrives into the encrypted
cache. No redaction, no Claude, no network egress -- those belong to the batch
job. Keeping this process minimal is deliberate: it is the one thing that must
never stop, and every dependency it does not have is a way it cannot fail.
"""

from __future__ import annotations

import logging
import signal
import sys

from .cache import Cache
from .config import Config, ConfigError
from .identity import Roster
from .logging_setup import configure
from .receiver import Receiver

log = logging.getLogger(__name__)


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure(cfg.log_level)

    cache = Cache.open(cfg.cache_path, cfg.cache_key)

    # Loaded so an opt-out is honoured at ingest, as PRIVACY.md says it is.
    # A missing roster is NOT fatal here: the receiver's job is to lose no
    # messages, and refusing to start would do more harm than running with the
    # opt-out enforced one stage later, where transcript.Builder also applies
    # it. The batch job does refuse to run without a roster, which is where
    # redaction actually needs it.
    try:
        roster = Roster.load(cfg.roster_path)
    except FileNotFoundError:
        roster = None
        log.warning(
            "no roster; opt-out will be applied at transcript build instead of ingest",
            extra={"path": cfg.roster_path.name},
        )

    receiver = Receiver(
        cfg.signal_host, cfg.signal_port, cfg.group_id, cache, roster,
        # Lets the operator discover what members are called without knowing
        # anyone's real name, and without a second connection to the daemon.
        observed_handles_path=cfg.roster_path.parent / "observed-handles.json",
    )

    def shutdown(signum, _frame):
        # Stop after the current frame so a message in flight is written first.
        log.info("shutdown requested", extra={"signal": signum})
        receiver.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info(
        "receiver starting",
        extra={"host": cfg.signal_host, "port": cfg.signal_port,
               "counts": cache.counts()},
    )
    try:
        receiver.run_forever()
    finally:
        log.info("receiver stopped", extra=receiver.stats.as_dict())
        cache.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
