"""One-time (re-runnable) loudness boost for episodes already in the Plex folder.

Backs up each .m4a to data/audio_backup_pre_boost/ (OUTSIDE the Plex-scanned path,
so Plex won't index the backups), boosts it in place via rabbit_hole.audio, then
refreshes Plex. Backups are skipped if already present, but files ARE re-boosted on
every run — run it once per intended boost.

Usage:  uv run python scripts/boost_existing_episodes.py
"""
import sys
from pathlib import Path

import keyring

from rabbit_hole.audio import boost_folder
from rabbit_hole.config import load_config
from rabbit_hole.plex import _refresh_plex

CONFIG_PATH = Path("config/config.toml")
BACKUP_DIR = Path("data/audio_backup_pre_boost")
KEYRING_SERVICE = "rabbit_hole"


def main() -> int:
    cfg = load_config(CONFIG_PATH)
    target = Path(cfg.plex.target_folder)
    print(f"Boosting *.m4a in {target} by x{cfg.audio.gain_factor} "
          f"(backups -> {BACKUP_DIR})")

    boosted, failed = boost_folder(target, BACKUP_DIR, gain=cfg.audio.gain_factor)
    for name in boosted:
        print(f"  boosted  {name}")
    for name, err in failed:
        print(f"  FAILED   {name}: {err}")
    print(f"\n{len(boosted)} boosted, {len(failed)} failed.")

    if not boosted:
        return 1 if failed else 0

    token = keyring.get_password(KEYRING_SERVICE, "plex_token") or ""
    if token:
        try:
            _refresh_plex(cfg.plex, token)
            print("Plex refreshed.")
        except Exception as e:  # noqa: BLE001 - refresh is best-effort
            print(f"Plex refresh failed (non-fatal): {e}")
    else:
        print("No plex_token in keyring; skipped Plex refresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
