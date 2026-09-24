"""Read-only application-identity probe; never logs credentials or record contents."""
import argparse
import json
from pathlib import Path
import lark_oapi as lark
from lark_oapi.api.bitable.v1 import ListAppTableFieldRequest
from invoice_print_layout.bot import read_bot_settings, read_bot_secret

parser = argparse.ArgumentParser()
parser.add_argument('--workspace', type=Path, default=Path('workspace'))
args = parser.parse_args()
config = json.loads((args.workspace/'lodging.json').read_text(encoding='utf-8'))
settings = read_bot_settings(args.workspace/'feishu_bot.toml')
client = lark.Client.builder().app_id(settings.app_id).app_secret(read_bot_secret(settings.app_id)).timeout(15).log_level(lark.LogLevel.ERROR).build()
request = ListAppTableFieldRequest.builder().app_token(config['base_token']).table_id(config['table_id']).page_size(100).build()
try:
    result = client.bitable.v1.app_table_field.list(request)
    print(json.dumps({'success': result.success(), 'code': result.code,
        'fields': [f.field_name for f in result.data.items] if result.success() else [],
        'note': 'Read-only schema probe; write permission and end-to-end booking are not verified.'}, ensure_ascii=False))
    raise SystemExit(0 if result.success() else 1)
except (OSError, TimeoutError):
    print(json.dumps({'success': False, 'note': 'Network unavailable; no writes attempted.'}))
    raise SystemExit(1)
