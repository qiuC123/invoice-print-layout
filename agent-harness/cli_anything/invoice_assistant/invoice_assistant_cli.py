import json
from pathlib import Path
import shlex
import sys
import click

from cli_anything.invoice_assistant import __version__
from cli_anything.invoice_assistant.core import reports, workflow as flow
from cli_anything.invoice_assistant.utils.client import Client, CLIError, configuration, read_json


def emit(ctx, value):
    result = {'ok': True, **value, 'operations': dict(ctx.obj['client'].operations)}
    if ctx.obj.get('project'): result['project_id'] = ctx.obj['project']
    click.echo(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=None if ctx.obj['json'] else 2))


@click.group(invoke_without_command=True)
@click.option('--json', 'as_json', is_flag=True, help='Output JSON; place before subcommands.')
@click.option('--dry-run', is_flag=True, help='Read/validate without POST or saving output.')
@click.option('--url', help='Original loopback invoice service root URL.')
@click.option('--workspace-id', help='Pinned workspace identity from original /api/runtime.')
@click.option('--project', help='Stable original invoice project ID, required for project actions.')
@click.version_option(__version__)
@click.pass_context
def cli(ctx, as_json, dry_run, url, workspace_id, project):
    """发票助手CLI：已有识别/人工分类优先，直连原服务，无需个人工作台。"""
    obj = ctx.ensure_object(dict); prior = obj.get('config', {})
    if not url: url = prior.get('url')
    if not workspace_id: workspace_id = prior.get('workspace_id')
    config = configuration(url, workspace_id)
    obj.update(json=as_json or obj.get('json', False), dry_run=dry_run or obj.get('dry_run', False),
               config=config, client=Client(config), project=project or obj.get('project'))
    if ctx.invoked_subcommand is None: ctx.invoke(repl)


@cli.command()
@click.pass_context
def doctor(ctx):
    """Check original runtime and saved state, without recognition/classification."""
    client = ctx.obj['client']; rt = client.runtime(); state = client.state()
    emit(ctx, {'status': 'ready', 'runtime': flow.pick(rt, ('app_id', 'ready', 'workspace_id', 'build_id', 'app_mode')),
               'projects': len(state['projects']), 'expenses': len(state['items']), 'workbench_required': False,
               'recognition_policy': 'original local first; GLM fallback only when explicitly processing missing recognition',
               'classification_policy': 'original human/rules/cache first; Jev only on explicit missing classification'})


@cli.group()
def projects(): """List projects; IDs remain owned by the original service."""


@projects.command('list')
@click.pass_context
def list_projects(ctx):
    emit(ctx, {'status': 'complete', 'projects': [flow.pick(p, ('id', 'name', 'revision', 'reimbursement_status')) for p in ctx.obj['client'].state()['projects']], 'reused': True})


@cli.group()
def project(): """Inspect the selected original project."""


@project.command('inspect')
@click.pass_context
def inspect_project(ctx): emit(ctx, flow.inspect_project(ctx.obj['client'], ctx.obj['project']))


def paging(function):
    function = click.option('--limit', type=click.IntRange(1, 1000), default=200)(function)
    return click.option('--offset', type=click.IntRange(0), default=0)(function)


@cli.group()
def materials(): """Saved material results; no implicit OCR or GLM."""


@materials.command('list')
@paging
@click.pass_context
def list_materials(ctx, limit, offset):
    rows = flow.material_rows(ctx.obj['client'], ctx.obj['project'])
    emit(ctx, {'status': 'complete', 'total': len(rows), 'offset': offset, 'has_more': offset + limit < len(rows),
               'materials': [flow.material_view(r) for r in rows[offset:offset+limit]], 'reused': True})


@materials.command('show')
@click.argument('id')
@click.pass_context
def show_material(ctx, id):
    row = flow.choose(flow.material_rows(ctx.obj['client'], ctx.obj['project']), [id])[0]
    emit(ctx, {'status': 'complete', 'material': flow.material_view(row), 'reused': True})


@materials.command('prepare')
@click.option('--id', 'ids', multiple=True, required=True)
@click.option('--recognize-missing', is_flag=True, help='Explicitly process only unsaved entries via original local OCR/GLM fallback.')
@click.pass_context
def prepare(ctx, ids, recognize_missing):
    emit(ctx, flow.prepare(ctx.obj['client'], ctx.obj['project'], ids, recognize_missing, ctx.obj['dry_run']))


@materials.command('receive')
@click.option('--file', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def receive(ctx, file): emit(ctx, flow.receive(ctx.obj['client'], ctx.obj['project'], file, ctx.obj['dry_run']))


@cli.group()
def expenses(): """Original expenses, manual categories and review state."""


@expenses.command('list')
@paging
@click.pass_context
def list_expenses(ctx, limit, offset):
    client = ctx.obj['client']; rows = flow.expense_rows(client, ctx.obj['project'])
    emit(ctx, {'status': 'complete', 'total': len(rows), 'offset': offset, 'has_more': offset + limit < len(rows),
               'summary': flow.summary(rows), 'expenses': [flow.expense_view(r, client.state()) for r in rows[offset:offset+limit]], 'reused': True})


@expenses.command('show')
@click.argument('id')
@click.pass_context
def show_expense(ctx, id):
    client = ctx.obj['client']; row = flow.choose(flow.expense_rows(client, ctx.obj['project']), [id])[0]
    emit(ctx, {'status': 'complete', 'expense': flow.expense_view(row, client.state()), 'reused': True})


@expenses.command('classify')
@click.option('--id', 'ids', multiple=True, required=True)
@click.option('--classify-missing', is_flag=True, help='Explicitly ask original classifier only for unsaved, unreviewed records.')
@click.pass_context
def classify(ctx, ids, classify_missing):
    emit(ctx, flow.classify(ctx.obj['client'], ctx.obj['project'], ids, classify_missing, ctx.obj['dry_run']))


@cli.group()
def matches(): """Original local matching rules; no duplicate recognizer."""


@matches.command('list')
@click.pass_context
def list_matches(ctx): emit(ctx, {'status': 'complete', 'board': flow.matches(ctx.obj['client'], ctx.obj['project']), 'reused': True})


@matches.command('suggest')
@click.pass_context
def suggest_matches(ctx): emit(ctx, {'status': 'complete', 'suggestions': flow.matches(ctx.obj['client'], ctx.obj['project'], suggestions=True), 'reused': True})


@cli.group()
def drafts(): """Create unverified drafts through native atomic/idempotent confirmation."""


@drafts.command('create')
@click.option('--request', 'request_file', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option('--request-id', required=True, help='Stable idempotency key, 8-128 ASCII identifier characters.')
@click.pass_context
def create_drafts(ctx, request_file, request_id):
    emit(ctx, flow.create_drafts(ctx.obj['client'], ctx.obj['project'], read_json(request_file), request_id, ctx.obj['dry_run']))


@cli.group('reports')
def reports_group(): """List/download existing original report snapshots; no generation or submission."""


@reports_group.command('list')
@click.pass_context
def list_reports(ctx): emit(ctx, reports.listing(ctx.obj['client'], ctx.obj['project']))


@reports_group.command('download')
@click.option('--report-id', required=True)
@click.option('--output-dir', required=True, type=click.Path(path_type=Path))
@click.pass_context
def download_report(ctx, report_id, output_dir):
    emit(ctx, reports.download(ctx.obj['client'], ctx.obj['project'], report_id, output_dir, ctx.obj['dry_run']))


@cli.group('results')
def results_group(): """Offline verification of already downloaded report results."""


@results_group.command('show')
@click.argument('directory', type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.pass_context
def show_result(ctx, directory): emit(ctx, reports.public(reports.read(directory), directory.resolve()))


@cli.command()
@click.pass_context
def repl(ctx):
    """Optional REPL; project selection is process local, business state stays native."""
    from cli_anything.invoice_assistant.utils.repl_skin import ReplSkin
    interactive = sys.stdin.isatty() and not ctx.obj['json']
    skin = ReplSkin('invoice-assistant', version=__version__) if interactive else None
    if interactive: skin.print_banner()
    elif ctx.obj['json']: emit(ctx, {'status': 'ready', 'mode': 'repl'})
    while True:
        try: line = skin.get_input(None) if interactive else sys.stdin.readline()
        except (EOFError, KeyboardInterrupt): break
        if not line: break
        line = line.strip()
        if line in ('quit', 'exit'): break
        if not line: continue
        try:
            args = shlex.split(line, posix=sys.platform != 'win32')
            if sys.platform == 'win32': args = [a[1:-1] if len(a)>1 and a[0]==a[-1] and a[0] in '\"\'' else a for a in args]
            cli.main(args=['--help'] if args == ['help'] else args, standalone_mode=False, obj=ctx.obj)
        except (CLIError, click.ClickException) as exc: error(exc, ctx.obj['json'])
        except click.exceptions.Exit: pass
    if interactive: skin.print_goodbye()


def error(exc, as_json):
    body = {'ok': False, 'status': 'failed', 'error': {'code': getattr(exc, 'code', 'INVALID_REQUEST'), 'message': str(exc)}}
    click.echo(json.dumps(body, ensure_ascii=False) if as_json else '错误：' + str(exc))


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'): stream.reconfigure(encoding='utf-8')
    try: cli.main(standalone_mode=False)
    except click.exceptions.Exit as exc: raise SystemExit(exc.exit_code)
    except (CLIError, click.ClickException) as exc:
        error(exc, '--json' in sys.argv); raise SystemExit(getattr(exc, 'exit_code', 2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        error(CLIError('本地状态或原服务结果处理失败：' + type(exc).__name__, 'INVALID_STATE', 1), '--json' in sys.argv); raise SystemExit(1)


if __name__ == '__main__': main()
