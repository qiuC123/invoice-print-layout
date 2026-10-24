"""Offline evaluation: --copy-from SOURCE --workspace NEW_COPY, then evaluate COPY."""
from pathlib import Path
import argparse
import json
import socket

from invoice_print_layout.recognition_eval import copy_workspace, evaluate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--copy-from', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    def offline(*_args: object, **_kwargs: object) -> None:
        raise OSError('Recognition evaluation is offline')
    socket.socket.connect = offline  # type: ignore[method-assign,assignment]
    socket.create_connection = offline  # type: ignore[assignment]
    if args.copy_from:
        copy_workspace(args.copy_from, args.workspace)
    output = (args.output or args.workspace / 'recognition-eval.json').resolve()
    if not output.is_relative_to(args.workspace.resolve()):
        raise ValueError('评估输出必须保存在副本目录内')
    result = evaluate(args.workspace)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'samples': result['samples'], 'oracle_unchanged': result['oracle_unchanged'], 'output': str(output)}))


if __name__ == '__main__':
    main()
