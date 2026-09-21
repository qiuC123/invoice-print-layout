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
app.add_typer(mail_app, name="mail")
app.add_typer(bot_app, name="bot")


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
