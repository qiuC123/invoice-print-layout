from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from invoice_print_layout.bot import (
    BotError,
    configure_bot,
    new_pairing_code,
    read_bot_secret,
    read_bot_settings,
)
from invoice_print_layout.mail163 import (
    MailImportError,
    configure_mail,
    import_pdf_attachments,
    read_auth_code,
    read_mail_settings,
)
from invoice_print_layout.storage import (
    ensure_workspace,
    read_person_name,
    write_person_name,
)
from invoice_print_layout.workflow import process_workspace


app = typer.Typer(
    no_args_is_help=True,
    help="在本机接收、整理网约车票据并生成 A4 打印包。",
)
mail_app = typer.Typer(help="配置 163 邮箱并只读导入发票 PDF 附件。")
bot_app = typer.Typer(help="配置并运行飞书票据机器人。")
daily_app = typer.Typer(help="每日施工日报：限定采集、证据核验、HTML/PPT与原生检查。")
settlement_app = typer.Typer(help="结算照片核对草稿：已确认材料、原模板表格和可追溯验收。")
app.add_typer(mail_app, name="mail")
app.add_typer(bot_app, name="bot")
app.add_typer(daily_app, name="daily")
app.add_typer(settlement_app, name="settlement")


@daily_app.command("chat-pack")
def daily_chat_pack_command(
    receipts: Annotated[list[Path], typer.Option("--receipt")],
    destination: Annotated[Path, typer.Option("--destination")],
    project_id: Annotated[str, typer.Option("--project")],
    project_title: Annotated[str, typer.Option("--title")],
    batch_id: Annotated[str, typer.Option("--batch")],
    supplements: Annotated[Path | None, typer.Option("--supplements")] = None,
) -> None:
    """冻结采集聊天、照片和缺项，建立WorkBuddy用途初筛包。"""
    import json
    from invoice_print_layout.chat_handoff import prepare_chat
    try:
        result = prepare_chat(receipts, destination, project_id, project_title, batch_id, supplements=supplements)
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@daily_app.command("dispatch-chat")
def daily_dispatch_chat_command(
    package: Annotated[Path, typer.Option("--package")],
    desktop_config: Annotated[Path, typer.Option("--desktop-config")],
) -> None:
    """显式操作已登录WorkBuddy桌面，一次派发当前资料包。"""
    import json
    from invoice_print_layout.workbuddy_desktop_handoff import dispatch_chat
    try:
        result = dispatch_chat(package, desktop_config)
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result['state'] not in ('waiting', 'received'):
        raise typer.Exit(2)


@daily_app.command("receive-chat")
def daily_receive_chat_command(
    package: Annotated[Path, typer.Option("--package")],
    wait_seconds: Annotated[int, typer.Option("--wait-seconds")] = 0,
) -> None:
    """核验WB完整回传并生成Codex接收清单和照片预览。"""
    import json
    from invoice_print_layout.workbuddy_desktop_handoff import poll_chat
    try:
        result = poll_chat(package, wait_seconds)
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result['state'] != 'received':
        raise typer.Exit(2)


@daily_app.command("role-pack")
def daily_role_pack_command(
    catalog: Annotated[Path, typer.Option("--catalog")],
    destination: Annotated[Path, typer.Option("--destination")],
) -> None:
    """冻结Codex核对的照片时间清单，生成WorkBuddy岗位填表包。"""
    import json
    from invoice_print_layout.personnel_handoff import prepare_package
    try:
        result = prepare_package(catalog, destination)
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@daily_app.command("seal-roles")
def daily_seal_roles_command(
    package: Annotated[Path, typer.Option("--package")],
) -> None:
    """岗位CSV填写完成后核验并写入完成标记；不调用分类服务。"""
    import json
    from invoice_print_layout.personnel_handoff import seal_package
    try:
        result = seal_package(package)
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@daily_app.command("receive-roles")
def daily_receive_roles_command(
    package: Annotated[Path, typer.Option("--package")],
    output_root: Annotated[Path, typer.Option("--output-root")],
    registry: Annotated[Path, typer.Option("--registry")] = Path("workspace/construction-reports/registry.json"),
) -> None:
    """接收完成包与用户纠正，生成按时间排序的XLSX/CSV/JSON和照片预览。"""
    import json
    from invoice_print_layout.construction_reports import read_object
    from invoice_print_layout.personnel_handoff import receive_package
    try:
        result = receive_package(package, output_root, read_object(registry)["runtime"])
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@daily_app.command("correct-role")
def daily_correct_role_command(
    package: Annotated[Path, typer.Option("--package")],
    output_root: Annotated[Path, typer.Option("--output-root")],
    photo_id: Annotated[str, typer.Option("--photo-id")],
    role: Annotated[str, typer.Option("--role")],
    basis: Annotated[str, typer.Option("--basis")],
    note: Annotated[str, typer.Option("--note")] = "",
) -> None:
    """保存用户已明确确认的岗位纠正，下一次接收优先应用。"""
    import json
    from invoice_print_layout.personnel_handoff import correct_role
    try:
        result = correct_role(package, output_root, photo_id, role, basis, note)
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@daily_app.command("run")
def daily_run_command(
    project: Annotated[str, typer.Option("--project", "-p")],
    report_date: Annotated[str, typer.Option("--date", "-d")],
    registry: Annotated[Path, typer.Option("--registry")] = Path("workspace/construction-reports/registry.json"),
    manifest: Annotated[Path | None, typer.Option("--manifest")] = None,
    output_root: Annotated[Path | None, typer.Option("--output-root")] = None,
) -> None:
    """按项目/日期调用；缺少当天确认资料时返回具体缺项。"""
    import json
    from invoice_print_layout.construction_reports import run_report
    try:
        result = run_report(registry, project, report_date, manifest_path=manifest, output_root=output_root)
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["state"] == "needs_input":
        raise typer.Exit(2)


@daily_app.command("collect")
def daily_collect_command(
    project: Annotated[str, typer.Option("--project", "-p")],
    report_date: Annotated[str, typer.Option("--date", "-d")],
    registry: Annotated[Path, typer.Option("--registry")] = Path("workspace/construction-reports/registry.json"),
    refresh: Annotated[bool, typer.Option("--refresh", help="新建采集批次，保留旧结果。")] = False,
    recover_missing: Annotated[bool, typer.Option("--recover-missing", help="允许操作微信窗口补齐缺图。")] = False,
    desktop_config: Annotated[Path | None, typer.Option("--desktop-config", help="本机补图配置JSON。")] = None,
) -> None:
    """采集已登记会话；可显式启用桌面补图，不发送或改变调度。"""
    import json
    from invoice_print_layout.construction_report_collect import collect
    from invoice_print_layout.construction_reports import local_path, project_config
    try:
        config, config_path, _ = project_config(registry.resolve(), project, report_date)
        source = dict(config["source"])
        source["reader_config"] = str(local_path(config_path.parent, source["reader_config"]))
        output = config_path.parent / "collections" / report_date
        if recover_missing:
            from invoice_print_layout.construction_photo_recovery import collect_recovering
            if desktop_config is None:
                raise ValueError("自动补图需要--desktop-config指定本机配置")
            result = collect_recovering(source, report_date, output, desktop_config.resolve(), refresh=refresh)
        else:
            if desktop_config is not None:
                raise ValueError("--desktop-config须与--recover-missing一起使用")
            result = collect(source, report_date, output, refresh=refresh)
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["state"] != "done":
        raise typer.Exit(2)


@daily_app.command("review")
def daily_review_command(
    run: Annotated[Path, typer.Option("--run")],
    reviewer: Annotated[str, typer.Option("--reviewer")],
    evidence: Annotated[str, typer.Option("--evidence")],
) -> None:
    """实际逐页检查之后登记回执；不会自动确认未查看的页面。"""
    import json
    from invoice_print_layout.construction_reports import record_visual_review
    try:
        result = record_visual_review(run.resolve(), reviewer, evidence)
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@daily_app.command("status")
def daily_status_command(run: Annotated[Path, typer.Option("--run")]) -> None:
    """只读已有运行状态，不启动服务。"""
    import json
    from invoice_print_layout.construction_reports import read_object
    try:
        result = read_object(run.resolve() / "state.json")
    except (ValueError, OSError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@settlement_app.command("run")
def settlement_run_command(
    project: Annotated[str, typer.Option("--project", "-p")],
    manifest: Annotated[Path, typer.Option("--manifest")],
    registry: Annotated[Path, typer.Option("--registry")] = Path("workspace/construction-reports/settlement-registry.json"),
    output_root: Annotated[Path | None, typer.Option("--output-root")] = None,
) -> None:
    """根据已确认清单生成照片核对草稿，不填正式数量、人天或费用。"""
    import json
    from invoice_print_layout.settlement_reports import run_report
    try:
        result = run_report(registry, project, manifest, output_root=output_root)
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["state"] == "needs_input":
        raise typer.Exit(2)


@settlement_app.command("status")
def settlement_status_command(run: Annotated[Path, typer.Option("--run")]) -> None:
    """只读结算草稿状态，不渲染、不发送。"""
    import json
    from invoice_print_layout.settlement_reports import status
    try:
        result = status(run.resolve())
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@settlement_app.command("review")
def settlement_review_command(
    run: Annotated[Path, typer.Option("--run")],
    reviewer: Annotated[str, typer.Option("--reviewer")],
    evidence: Annotated[str, typer.Option("--evidence")],
) -> None:
    """实际逐页查看之后登记完成；草稿完成不代表正式结算提交。"""
    import json
    from invoice_print_layout.settlement_reports import record_visual_review
    try:
        result = record_visual_review(run.resolve(), reviewer, evidence)
    except (ValueError, OSError, KeyError) as exc:
        _abort(str(exc))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


def _abort(message: str) -> NoReturn:
    typer.echo(f"错误：{message}", err=True)
    raise typer.Exit(code=1)


@app.callback()
def main() -> None:
    """网约车行程单与电子发票 A4 排版工具。"""


@mail_app.command("setup")
def mail_setup_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="票据工作区目录。"),
    ] = Path("workspace"),
    folder: Annotated[
        str,
        typer.Option("--folder", help="只读检索的邮箱文件夹。"),
    ] = "INBOX",
) -> None:
    """保存邮箱地址和客户端授权码；授权码进入 Windows 凭据管理器。"""
    paths = ensure_workspace(workspace)
    address = typer.prompt("请输入完整的 163 邮箱地址").strip()
    auth_code = typer.prompt(
        "请输入客户端授权码（不是邮箱登录密码）",
        hide_input=True,
        confirmation_prompt="请再次输入客户端授权码",
    )
    try:
        settings = configure_mail(paths.mail_config, address, folder, auth_code)
    except MailImportError as exc:
        _abort(str(exc))
    typer.echo(f"163 邮箱已配置：{settings.address}；只读文件夹：{settings.folder}")


@mail_app.command("import")
def mail_import_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="票据工作区目录。"),
    ] = Path("workspace"),
    days: Annotated[
        int,
        typer.Option("--days", min=1, max=365, help="向前检索的天数。"),
    ] = 30,
) -> None:
    """从 163 邮箱只读下载发票和行程 PDF 到“待处理”。"""
    paths = ensure_workspace(workspace)
    try:
        settings = read_mail_settings(paths.mail_config)
        auth_code = read_auth_code(settings.address)
        summary = import_pdf_attachments(paths, settings, auth_code, days)
    except MailImportError as exc:
        _abort(str(exc))
    typer.echo(
        f"邮件导入完成：新增 {summary.downloaded}，重复 {summary.duplicates}，"
        f"无相关附件 {summary.ignored}，无效 PDF {summary.invalid_pdf}"
    )


@bot_app.command("setup")
def bot_setup_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="票据工作区目录。"),
    ] = Path("workspace"),
) -> None:
    """保存飞书自建应用凭据；App Secret 进入 Windows 凭据管理器。"""
    paths = ensure_workspace(workspace)
    app_id = typer.prompt("请输入飞书 App ID").strip()
    app_secret = typer.prompt(
        "请输入飞书 App Secret",
        hide_input=True,
        confirmation_prompt="请再次输入飞书 App Secret",
    )
    try:
        settings = configure_bot(paths.root / "feishu_bot.toml", app_id, app_secret)
    except BotError as exc:
        _abort(str(exc))
    binding = "已保留原有绑定" if settings.owner_open_id else "尚未绑定使用者"
    typer.echo(f"飞书机器人已配置：{settings.app_id}；{binding}")


@bot_app.command("run")
def bot_run_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="票据工作区目录。"),
    ] = Path("workspace"),
    debug: Annotated[
        bool,
        typer.Option("--debug/--no-debug", help="显示飞书 SDK 调试日志。"),
    ] = False,
) -> None:
    """通过长连接运行飞书机器人；按 Ctrl+C 停止。"""
    paths = ensure_workspace(workspace)
    config_path = paths.root / "feishu_bot.toml"
    try:
        settings = read_bot_settings(config_path)
        app_secret = read_bot_secret(settings.app_id)
    except BotError as exc:
        _abort(str(exc))

    pairing_code = None if settings.owner_open_id else new_pairing_code()
    if pairing_code:
        typer.echo(f"首次绑定码：{pairing_code}")
        typer.echo(f"请在飞书中给机器人发送：绑定 {pairing_code}")
    else:
        typer.echo("机器人已绑定，仅接受已绑定使用者的单聊消息。")
    typer.echo("飞书机器人正在连接；按 Ctrl+C 停止。")
    try:
        from invoice_print_layout.feishu_bot import run_feishu_bot

        run_feishu_bot(
            paths.root,
            config_path,
            settings,
            app_secret,
            pairing_code,
            debug,
        )
    except ImportError as exc:
        _abort(f"缺少飞书 SDK，请重新安装项目：{exc}")
    except RuntimeError as exc:
        _abort(str(exc))
    except KeyboardInterrupt:
        typer.echo("飞书机器人已停止。")


@app.command("process")
def process_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="票据工作区目录。"),
    ] = Path("workspace"),
    open_completed: Annotated[
        bool,
        typer.Option("--open-completed/--no-open-completed", help="完成后打开已完成目录。"),
    ] = True,
) -> None:
    """处理工作区待处理目录中的全部 PDF。"""
    paths = ensure_workspace(workspace)
    person_name = read_person_name(paths.config)
    if person_name is None:
        person_name = typer.prompt("请输入用于输出文件名的姓名").strip()
        if not person_name:
            raise typer.BadParameter("姓名不能为空")
        write_person_name(paths.config, person_name)

    paths, summary = process_workspace(paths.root, person_name)
    typer.echo(
        f"处理完成：成功 {summary.success_count}，"
        f"需要检查 {summary.review_count}，重复 {summary.duplicate_count}"
    )
    typer.echo(f"处理记录：{paths.latest_report}")
    if open_completed and os.name == "nt":
        os.startfile(paths.completed)


if __name__ == "__main__":
    app()
