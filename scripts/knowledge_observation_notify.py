"""Deliver a persisted observation report through the existing biomed mail channel."""

import argparse
import fcntl
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.pubtator import atomic_json, read
from scripts.watch_literature import notify


def deliver(root, observation, mail_config, *, send=False):
    observation = Path(observation)
    observation.mkdir(parents=True, exist_ok=True)
    with (observation / "notification.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = observation / "notification-state.json"
        state = read(state_path) if state_path.exists() else {}
        if state.get("notices", {}).get("observation-report", {}).get("sent_at"):
            return "already_sent"
        subprocess.run([sys.executable, "-B", str(ROOT / "scripts/knowledge_observation_report.py"),
                        "--root", str(root), "--observation", str(observation)], check=True, cwd=ROOT)
        if not send:
            return "preview_only"
        body = (observation / "report.md").read_text(encoding="utf-8")
        body += "\n\n工作流：http://192.168.124.2:50011/management/workflows/\n阶段汇总不会暂停后台处理。"
        notify({"mail_config": str(mail_config), "collection_id": observation.name}, state,
               "observation-report", "夜间扩大测试阶段汇总", body, time.time())
        atomic_json(state_path, state)
        if not state.get("notices", {}).get("observation-report", {}).get("sent_at"):
            raise RuntimeError("Observation email not accepted; persisted state will retry")
        return "accepted_by_mail_server"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--mail-config", type=Path, required=True)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()
    print(deliver(args.root, args.observation, args.mail_config, send=args.send))


if __name__ == "__main__":
    main()
