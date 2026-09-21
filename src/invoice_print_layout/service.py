"""Hidden Windows supervisor; credentials stay in the logged-in user's keyring."""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from invoice_print_layout.reliability import save_json, single_instance


def supervise(workspace: Path) -> None:
    workspace = workspace.resolve()
    with single_instance(workspace, 'bot-service.lock'):
        logs = workspace / '运行日志'
        logs.mkdir(exist_ok=True)
        handler = RotatingFileHandler(logs / 'supervisor.log', maxBytes=2_000_000,
                                      backupCount=3, encoding='utf-8')
        logging.basicConfig(level=logging.INFO, handlers=[handler],
                            format='%(asctime)s %(levelname)s %(message)s', force=True)
        executable = Path(sys.executable).with_name('python.exe') if os.name == 'nt' else Path(sys.executable)
        delay = 5
        while True:
            child_log = logs / 'bot.log'
            if child_log.exists() and child_log.stat().st_size > 5_000_000:
                os.replace(child_log, logs / 'bot.previous.log')
            with child_log.open('ab') as output:
                process = subprocess.Popen(
                    [str(executable), '-u', '-m', 'invoice_print_layout.cli',
                     'bot', 'run', '--workspace', str(workspace)],
                    stdout=output, stderr=output,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                    env={**os.environ, 'PYTHONUTF8': '1'},
                )
                save_json(workspace / 'bot-service.json', {
                    'supervisor_pid': os.getpid(), 'child_pid': process.pid,
                    'started_at': time.time(), 'state': 'running',
                })
                logging.info('Started bot pid=%s', process.pid)
                started = time.monotonic()
                code = process.wait()
            logging.warning('Bot exited code=%s; restart in %ss', code, delay)
            save_json(workspace / 'bot-service.json', {
                'supervisor_pid': os.getpid(), 'child_pid': process.pid,
                'state': 'restarting', 'exit_code': code, 'restart_delay': delay,
            })
            time.sleep(delay)
            delay = 5 if time.monotonic() - started > 120 else min(delay * 2, 60)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    try:
        supervise(args.workspace)
    except RuntimeError:
        # Another supervisor owns this workspace; leave it running.
        return


if __name__ == '__main__':
    main()
