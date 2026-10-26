#!/usr/bin/env python3
"""Back up NetEase Cloud Music direct messages locally via self-hosted api-enhanced.

Dependencies: pip install -r requirements.txt
Usage:
  python backup_dm.py
  python backup_dm.py --uid 123456789 --download-media
Server: https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced (default localhost:3000)
Login cookies are held in process memory only and are never written to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

import qrcode
import requests

try:
    from generate_modern_html import process_directory as render_modern_html
except ImportError:
    render_modern_html = None

DEFAULT_API_BASE = os.environ.get("NETEASE_API", "http://127.0.0.1:3000").rstrip("/")
PAGE_SIZE = 50
MAX_PAGES = 10000
RETRY_LIMIT = 3
TIMEOUT = 30

# api-enhanced caches responses for 2 minutes keyed on host + URL + cookies,
# without including the POST body. Appending a monotonic sequence parameter
# differentiates pagination requests without requiring upstream config changes.
_CALL_SEQ = 0


def call(
    session: requests.Session,
    api_base: str,
    path: str,
    *,
    cookie: str | None = None,
    params: dict[str, Any] | None = None,
    method: str = "GET",
) -> dict:
    global _CALL_SEQ
    _CALL_SEQ += 1
    separator = "&" if "?" in path else "?"
    url = f"{api_base}{path}{separator}_nc={_CALL_SEQ}-{int(time.time() * 1000)}"
    for attempt in range(RETRY_LIMIT):
        try:
            if method == "POST":
                payload = dict(params or {})
                if cookie:
                    payload["cookie"] = cookie
                response = session.post(url, json=payload, timeout=TIMEOUT)
            else:
                response = session.get(url, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"接口返回格式异常: {str(data)[:300]}")
            return data
        except (requests.exceptions.RequestException, ValueError) as exc:
            if attempt == RETRY_LIMIT - 1:
                raise RuntimeError(f"请求接口 {path} 失败: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("请求失败")


def qr_auth(session: requests.Session, api_base: str, workdir: Path) -> str:
    key_res = call(session, api_base, "/login/qr/key", params={"timestamp": int(time.time() * 1000)})
    key = (key_res.get("data") or {}).get("unikey")
    if not key:
        raise RuntimeError("未获取到二维码密钥，请确认本地 API 服务运行正常。")

    qr_res = call(
        session,
        api_base,
        "/login/qr/create",
        params={"key": key, "platform": "web", "ua": "pc", "timestamp": int(time.time() * 1000)},
    )
    qr_url = (qr_res.get("data") or {}).get("qrurl")
    if not qr_url:
        raise RuntimeError("接口未返回二维码链接。")

    qr_path = workdir / "login_qr_temporary.png"
    qrcode.make(qr_url).save(qr_path)
    print(f"\n已生成临时登录二维码: {qr_path}")
    print("请使用网易云音乐 App 扫码并在手机端确认登录（验证完成后该图片将自动删除）。")
    webbrowser.open(qr_path.resolve().as_uri())

    try:
        for _ in range(100):  # ~5 minutes
            result = call(
                session,
                api_base,
                "/login/qr/check",
                params={
                    "key": key,
                    "ua": "pc",
                    "noCookie": "true",
                    "timestamp": int(time.time() * 1000),
                },
            )
            code = result.get("code")
            if code == 803:
                cookie = result.get("cookie")
                if not cookie:
                    raise RuntimeError("扫码确认成功，但接口未返回登录凭证。")
                print("扫码登录成功（凭证仅保存在当前进程内存中，不写入磁盘）。\n")
                return cookie
            if code == 800:
                raise RuntimeError("二维码已过期，请重新运行脚本。")
            if code not in (801, 802):
                print(f"登录状态码: {code}, 信息: {result.get('message', '')}")
            time.sleep(3)
        raise RuntimeError("扫码登录超时，请重新运行脚本。")
    finally:
        try:
            qr_path.unlink(missing_ok=True)
        except OSError:
            pass


def message_key(m: dict[str, Any]) -> str:
    for k in ("id", "msgId", "messageId", "msgid"):
        if m.get(k) is not None:
            return k + ":" + str(m[k])
    return hashlib.sha256(
        json.dumps(m, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    records.append(obj)
    return records


def millisecond_time(m: dict[str, Any]) -> int:
    try:
        return int(m.get("time", 0))
    except (ValueError, TypeError):
        return 0


def date_text(t: int) -> str:
    try:
        if t < 100000000000:
            t *= 1000
        return datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return str(t)


def decode_content(value: Any) -> tuple[str, str]:
    """Return readable text summary and full JSON payload."""
    decoded = value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass

    if isinstance(decoded, dict):
        parts = []
        msg = decoded.get("msg")
        if isinstance(msg, str) and msg.strip():
            parts.append(msg.strip())

        if isinstance(decoded.get("song"), dict):
            s = decoded["song"]
            artists = ", ".join(a.get("name", "") for a in s.get("artists", []) if a.get("name"))
            parts.append(f"[单曲分享] {s.get('name', '')}" + (f" - {artists}" if artists else ""))
        elif isinstance(decoded.get("playlist"), dict):
            pl = decoded["playlist"]
            parts.append(f"[歌单分享] {pl.get('name', '')}")
        elif isinstance(decoded.get("album"), dict):
            al = decoded["album"]
            parts.append(f"[专辑分享] {al.get('name', '')}")
        elif isinstance(decoded.get("picInfo"), dict):
            parts.append(f"[图片] {decoded['picInfo'].get('picUrl', '')}")
        elif isinstance(decoded.get("comment"), dict):
            c = decoded["comment"]
            parts.append(f"[评论分享] {c.get('content', '')}")
        elif isinstance(decoded.get("generalMsg"), dict):
            g = decoded["generalMsg"]
            parts.append(f"[{g.get('title') or '卡片消息'}]")

        plain = "\n".join(parts) if parts else "[特殊类型消息，详见 raw_messages.json]"
        detail = json.dumps(decoded, ensure_ascii=False, indent=2)
        return plain, detail

    if isinstance(decoded, str):
        return decoded, decoded
    return str(decoded), json.dumps(decoded, ensure_ascii=False, indent=2)


def sender_text(m: dict[str, Any]) -> str:
    obj = m.get("fromUser") or {}
    if isinstance(obj, dict):
        return str(obj.get("nickname") or obj.get("userId") or "未知用户")
    return str(obj) if obj else "未知用户"


def write_final(output_dir: Path, raw: list[dict[str, Any]], *, download_media: bool = False) -> None:
    unique = {message_key(m): m for m in raw}
    records = sorted(unique.values(), key=lambda x: (millisecond_time(x), message_key(x)))

    raw_file = output_dir / "raw_messages.json"
    txt_file = output_dir / "chat_history.txt"
    html_file = output_dir / "chat_history.html"

    raw_file.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    chunks = []
    cards = []
    for m in records:
        timestamp = date_text(millisecond_time(m))
        sender = sender_text(m)
        body, detail = decode_content(m.get("msg", ""))
        chunks.append(f"[{timestamp}] {sender}\n{body}\n")
        details_markup = ""
        if detail != body:
            details_markup = (
                "<details><summary>查看原始消息字段</summary><pre>"
                + html.escape(detail)
                + "</pre></details>"
            )
        cards.append(
            "<article class='message'><header>"
            + html.escape(sender)
            + " <time>"
            + html.escape(timestamp)
            + "</time></header><div class='body'>"
            + html.escape(body)
            + "</div>"
            + details_markup
            + "</article>"
        )

    txt_file.write_text("\n".join(chunks), encoding="utf-8")

    if render_modern_html:
        render_modern_html(output_dir, download_media=download_media, quiet=True)
    else:
        page = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>网易云音乐私信备份</title><style>
body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;background:#f4f5f7;color:#222;
max-width:850px;margin:24px auto;padding:0 18px}
h1{font-size:22px}.message{background:#fff;border:1px solid #e4e7ec;border-radius:11px;
padding:12px 17px;margin:11px 0}header{font-weight:600;margin-bottom:7px}
time{font-size:12px;color:#777;font-weight:400;margin-left:10px}.body{white-space:pre-wrap;overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f7f7;padding:10px}
summary{cursor:pointer;margin-top:12px;color:#555}footer{font-size:12px;color:#777;margin-top:20px}
</style></head><body><h1>网易云音乐私信备份</h1>
<p>本地离线备份。时间以运行导出程序的设备本地时区为准。</p>
"""
        page += "\n".join(cards)
        page += "<footer>部分语音、动态表情或已删除内容可能无法通过接口获取。</footer></body></html>"
        html_file.write_text(page, encoding="utf-8")

    earliest = date_text(millisecond_time(records[0])) if records else "N/A"
    latest = date_text(millisecond_time(records[-1])) if records else "N/A"
    print(f"\n导出完成，共保存 {len(records)} 条去重消息")
    print(f"时间范围: {earliest} ~ {latest}")
    print("输出文件:")
    print(f"  HTML 归档 : {html_file}")
    print(f"  纯文本记录: {txt_file}")
    print(f"  原始 JSON : {raw_file}")


def backup(
    session: requests.Session,
    api_base: str,
    cookie: str,
    uid: str,
    workdir: Path,
    *,
    download_media: bool = False,
) -> None:
    out = workdir / f"netease_dm_{uid}"
    out.mkdir(parents=True, exist_ok=True)
    part = out / "partial_messages.jsonl"
    saved = read_jsonl(part)
    if saved:
        cursor = min((millisecond_time(x) for x in saved if millisecond_time(x) > 0), default=0)
        print(f"检测到未完成的增量记录（已缓存 {len(saved)} 条），从时间戳 {cursor} 处继续拉取...")
    else:
        cursor = 0

    seen = {message_key(x) for x in saved}
    seen_cursors = set()
    reached_end = False

    try:
        for page_no in range(1, MAX_PAGES + 1):
            params: dict[str, Any] = {"uid": uid, "limit": PAGE_SIZE}
            if cursor:
                params["before"] = cursor
            response = call(session, api_base, "/msg/private/history", method="POST", params=params, cookie=cookie)
            if response.get("code") != 200:
                raise RuntimeError(
                    f"历史记录接口返回错误: code={response.get('code')}, "
                    f"message={response.get('message') or response.get('msg')}"
                )
            msgs = response.get("msgs")
            if not isinstance(msgs, list):
                raise RuntimeError(f"接口响应缺少 msgs 列表: keys={list(response.keys())}")
            if not msgs:
                reached_end = True
                print("接口返回空列表，已到达最早消息。")
                break

            added = 0
            with part.open("a", encoding="utf-8") as f:
                for msg in msgs:
                    if isinstance(msg, dict) and message_key(msg) not in seen:
                        seen.add(message_key(msg))
                        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                        added += 1

            timestamps = [millisecond_time(x) for x in msgs if isinstance(x, dict) and millisecond_time(x) > 0]
            if not timestamps:
                raise RuntimeError("返回消息缺少有效时间戳字段，无法继续安全翻页。")

            next_cursor = min(timestamps)
            print(
                f"第 {page_no} 页: 返回 {len(msgs)} 条，新增 {added} 条，"
                f"累计 {len(seen)} 条，当前最早 {date_text(next_cursor)}"
            )

            if response.get("more") is False:
                reached_end = True
                print("已拉取全部历史记录（more=false）。")
                break

            if next_cursor == cursor or next_cursor in seen_cursors or (cursor and next_cursor > cursor):
                raise RuntimeError("分页游标未继续向前推进，已停止拉取并保存当前数据。")

            seen_cursors.add(cursor)
            cursor = next_cursor
            time.sleep(0.8)
        else:
            raise RuntimeError("已达到最大分页上限，导出结果可能不完整。")
    except (KeyboardInterrupt, RuntimeError) as exc:
        print(f"\n拉取中断: {exc}", file=sys.stderr)
        print("已拉取的消息已写入本地增量文件，下次运行可自动续传。", file=sys.stderr)
    finally:
        records = read_jsonl(part)
        if records:
            write_final(out, records, download_media=download_media)
        if not reached_end:
            print("\n提示: 本次尚未确认拉取到历史最末端，数据可能不完整；重新运行可继续补齐。")


def main() -> None:
    parser = argparse.ArgumentParser(description="网易云音乐私信聊天记录本地备份工具。")
    parser.add_argument("--uid", type=str, default="", help="对话好友的数字 UID（留空则交互式输入）。")
    parser.add_argument("--api", type=str, default=DEFAULT_API_BASE, help="本地 api-enhanced 服务地址（默认 http://127.0.0.1:3000）。")
    parser.add_argument("--download-media", action="store_true", help="导出后自动下载头像、聊天图片与封面至本地 media/ 目录。")
    args = parser.parse_args()

    api_base = args.api.rstrip("/")
    print("网易云音乐私信本地备份工具")
    print(f"本地接口地址: {api_base}")

    try:
        uid = args.uid.strip()
        if not uid:
            uid = input("请输入对方的数字 UID（非昵称，可在对方主页分享链接 id= 后查看）: ").strip()
        if not re.fullmatch(r"[0-9]{1,20}", uid):
            raise RuntimeError("UID 必须为纯数字。")

        workdir = Path(__file__).resolve().parent
        with requests.Session() as session:
            session.trust_env = False  # 避免系统代理拦截 localhost 请求
            try:
                call(session, api_base, "/login/qr/key", params={"timestamp": int(time.time() * 1000)})
            except RuntimeError as exc:
                raise RuntimeError(
                    f"无法连接本地接口服务 ({api_base})，请先在独立终端启动 api-enhanced（node app.js）。"
                ) from exc

            cookie = qr_auth(session, api_base, workdir)
            backup(session, api_base, cookie, uid, workdir, download_media=args.download_media)
    except (RuntimeError, KeyboardInterrupt) as exc:
        print(f"\n错误: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
