#!/usr/bin/env python3
"""Render an offline interactive HTML archive from NetEase Cloud Music DM JSON data.

Usage:
  python generate_modern_html.py --dir netease_dm_<UID>
  python generate_modern_html.py --dir netease_dm_<UID> --download-media
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


def format_duration(ms: int) -> str:
    if not ms or ms <= 0:
        return ""
    total_sec = ms // 1000
    minutes = total_sec // 60
    seconds = total_sec % 60
    return f"{minutes:02d}:{seconds:02d}"


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url


def parse_raw_messages(raw_records: list[dict[str, Any]], target_uid: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Map for resolving message quotes
    id_map = {m.get("id"): m for m in raw_records if m.get("id")}
    
    # Identify users
    user_counts: dict[str, dict[str, Any]] = {}
    for m in raw_records:
        u = m.get("fromUser")
        if isinstance(u, dict):
            uid = str(u.get("userId"))
            if uid not in user_counts:
                user_counts[uid] = {
                    "userId": uid,
                    "nickname": u.get("nickname") or f"用户_{uid}",
                    "avatarUrl": normalize_url(u.get("avatarUrl")),
                    "count": 0,
                }
            user_counts[uid]["count"] += 1
            if not user_counts[uid]["avatarUrl"] and u.get("avatarUrl"):
                user_counts[uid]["avatarUrl"] = normalize_url(u.get("avatarUrl"))

    # Determine partner vs self
    if target_uid and target_uid in user_counts:
        partner_uid = target_uid
        self_candidates = [u for u in user_counts if u != partner_uid]
        self_uid = self_candidates[0] if self_candidates else None
    else:
        # Default: user with most messages or target
        sorted_users = sorted(user_counts.keys(), key=lambda k: user_counts[k]["count"], reverse=True)
        partner_uid = sorted_users[0] if sorted_users else "0"
        self_uid = sorted_users[1] if len(sorted_users) > 1 else None

    # Sort messages chronologically
    sorted_records = sorted(raw_records, key=lambda m: (int(m.get("time") or 0), str(m.get("id") or "")))

    parsed_messages = []
    all_songs = []
    all_pics = []
    hours_distribution = [0] * 24

    for m in sorted_records:
        mid = m.get("id")
        t = int(m.get("time") or 0)
        dt = datetime.fromtimestamp(t / 1000) if t > 0 else datetime.now()
        hours_distribution[dt.hour] += 1

        sender_obj = m.get("fromUser") or {}
        sender_uid = str(sender_obj.get("userId") if isinstance(sender_obj, dict) else "")
        sender_name = sender_obj.get("nickname") if isinstance(sender_obj, dict) else "未知用户"
        sender_avatar = normalize_url(sender_obj.get("avatarUrl")) if isinstance(sender_obj, dict) else ""
        if not sender_avatar and sender_uid in user_counts:
            sender_avatar = user_counts[sender_uid]["avatarUrl"]

        is_self = (sender_uid != partner_uid) if partner_uid else False

        # Decode msg body
        raw_msg = m.get("msg", "")
        decoded: dict[str, Any] = {}
        if isinstance(raw_msg, str):
            try:
                decoded = json.loads(raw_msg)
            except Exception:
                decoded = {"msg": raw_msg, "type": 6}
        elif isinstance(raw_msg, dict):
            decoded = raw_msg

        msg_type_code = decoded.get("type", 6)
        card_type = "text"
        card_data: dict[str, Any] = {}

        # Resolve Quote
        quote_info = None
        ref_data = m.get("refPrivateMsgData")
        ref_id = decoded.get("refMsgId")
        if ref_data:
            ref_user = (ref_data.get("fromUser") or {}).get("nickname") or "某人"
            ref_raw = ref_data.get("msg", "")
            try:
                ref_d = json.loads(ref_raw) if isinstance(ref_raw, str) else ref_raw
                ref_text = ref_d.get("msg") or ref_d.get("title") or "[卡片内容]"
            except Exception:
                ref_text = str(ref_raw)
            quote_info = {
                "refId": ref_data.get("id"),
                "sender": ref_user,
                "text": ref_text[:100] + ("..." if len(ref_text) > 100 else "")
            }
        elif ref_id and ref_id in id_map:
            orig = id_map[ref_id]
            ref_user = (orig.get("fromUser") or {}).get("nickname") or "某人"
            ref_raw = orig.get("msg", "")
            try:
                ref_d = json.loads(ref_raw) if isinstance(ref_raw, str) else ref_raw
                ref_text = ref_d.get("msg") or ref_d.get("title") or "[卡片内容]"
            except Exception:
                ref_text = str(ref_raw)
            quote_info = {
                "refId": ref_id,
                "sender": ref_user,
                "text": ref_text[:100] + ("..." if len(ref_text) > 100 else "")
            }

        # Handle specific message types
        if decoded.get("song"):
            # Music Song (typically type 1)
            card_type = "song"
            s = decoded.get("song") or {}
            artists = ", ".join([a.get("name", "") for a in s.get("artists", []) if a.get("name")])
            album_pic = normalize_url((s.get("album") or {}).get("picUrl") or (s.get("album") or {}).get("blurPicUrl"))
            card_data = {
                "songId": s.get("id"),
                "name": s.get("name") or "未知单曲",
                "artists": artists or "未知歌手",
                "album": (s.get("album") or {}).get("name") or "",
                "picUrl": album_pic,
                "duration": format_duration(s.get("duration", 0)),
                "link": f"https://music.163.com/#/song?id={s.get('id')}",
            }
            all_songs.append({
                **card_data,
                "time": dt.strftime("%Y-%m-%d %H:%M"),
                "sender": sender_name,
                "msgId": mid,
            })
        elif decoded.get("playlist"):
            # Playlist share (typically type 4)
            card_type = "song"
            pl = decoded.get("playlist") or {}
            creator = (pl.get("creator") or {}).get("nickname") or "歌单分享"
            track_cnt = pl.get("trackCount")
            cover_pic = normalize_url(pl.get("coverImgUrl") or pl.get("picUrl"))
            card_data = {
                "songId": f"pl_{pl.get('id')}",
                "name": pl.get("name") or "未知歌单",
                "artists": creator,
                "album": f"歌单 · {track_cnt} 首" if track_cnt else "歌单",
                "picUrl": cover_pic,
                "duration": "",
                "link": f"https://music.163.com/#/playlist?id={pl.get('id')}",
            }
        elif decoded.get("album"):
            # Album share (typically type 2)
            card_type = "song"
            al = decoded.get("album") or {}
            artist_name = (al.get("artist") or {}).get("name") or ", ".join(
                [a.get("name", "") for a in al.get("artists", []) if a.get("name")]
            )
            cover_pic = normalize_url(al.get("picUrl") or al.get("blurPicUrl"))
            card_data = {
                "songId": f"al_{al.get('id')}",
                "name": al.get("name") or "未知专辑",
                "artists": artist_name or "未知歌手",
                "album": "专辑",
                "picUrl": cover_pic,
                "duration": "",
                "link": f"https://music.163.com/#/album?id={al.get('id')}",
            }
        elif msg_type_code == 16 and decoded.get("picInfo"):
            # Picture / Meme
            card_type = "image"
            p = decoded.get("picInfo") or {}
            pic_url = normalize_url(p.get("picUrl"))
            w = p.get("width") or 0
            h = p.get("height") or 0
            card_data = {
                "url": pic_url,
                "width": w,
                "height": h,
                "format": p.get("format", "jpg")
            }
            all_pics.append({
                "url": pic_url,
                "time": dt.strftime("%Y-%m-%d %H:%M"),
                "sender": sender_name,
                "msgId": mid,
            })
        elif msg_type_code == 15 and decoded.get("comment"):
            # Comment share
            card_type = "comment"
            c = decoded.get("comment") or {}
            c_user = c.get("user") or {}
            card_data = {
                "content": c.get("content", ""),
                "resourceName": c.get("resourceName", ""),
                "author": c_user.get("nickname", "评论用户"),
                "avatar": normalize_url(c_user.get("avatarUrl")),
            }
        elif msg_type_code == 23 and decoded.get("generalMsg"):
            # Together Listen invite card
            card_type = "together"
            g = decoded.get("generalMsg") or {}
            card_data = {
                "title": g.get("title") or "加入一起听",
                "text": decoded.get("msg") or "邀请一起听歌",
                "cover": normalize_url(g.get("cover")),
                "notice": g.get("noticeMsg") or "网易云音乐 一起听",
            }
        else:
            text_body = decoded.get("msg", "")
            if not text_body and decoded.get("title"):
                text_body = decoded.get("title")
            if "不支持展示该内容" in str(text_body):
                card_type = "unsupported"
                card_data = {"notice": "[客户端特殊消息（如语音、动态表情或系统提示），接口未返回可展示正文]"}
            else:
                card_type = "text"
                card_data = {"text": str(text_body)}

        parsed_messages.append({
            "id": mid,
            "timestamp": t,
            "dateStr": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "dateDay": dt.strftime("%Y-%m-%d"),
            "dateTime": dt.strftime("%H:%M"),
            "senderId": sender_uid,
            "senderName": sender_name,
            "senderAvatar": sender_avatar,
            "isSelf": is_self,
            "type": card_type,
            "data": card_data,
            "quote": quote_info,
            "togetherSessionId": None,
        })

    # Identify Together Sessions (一起听会话切片)
    together_indices = [idx for idx, m in enumerate(parsed_messages) if m["type"] == "together"]
    together_sessions = []

    for session_num, start_pos in enumerate(together_indices, 1):
        start_msg = parsed_messages[start_pos]
        t0 = start_msg["timestamp"]
        curr_t = t0
        session_msgs = []
        session_songs = []
        pics_count = 0

        next_invite_pos = together_indices[session_num] if session_num < len(together_indices) else len(parsed_messages)

        for p in range(start_pos, min(next_invite_pos, len(parsed_messages))):
            m = parsed_messages[p]
            t = m["timestamp"]
            # 45-minute idle threshold
            if t - curr_t > 45 * 60 * 1000:
                break
            session_msgs.append(m["id"])
            m["togetherSessionId"] = session_num
            curr_t = t
            if m["type"] == "song":
                session_songs.append(m["data"].get("name"))
            elif m["type"] == "image":
                pics_count += 1

        dur_min = round((curr_t - t0) / 60000)
        together_sessions.append({
            "sessionId": session_num,
            "startMsgId": start_msg["id"],
            "startTime": start_msg["dateStr"],
            "durationMin": dur_min,
            "msgCount": len(session_msgs),
            "inviter": start_msg["senderName"],
            "songs": session_songs,
            "picsCount": pics_count,
            "msgIds": session_msgs,
        })

    # Stats Summary
    first_time = datetime.fromtimestamp(parsed_messages[0]["timestamp"] / 1000) if parsed_messages else datetime.now()
    last_time = datetime.fromtimestamp(parsed_messages[-1]["timestamp"] / 1000) if parsed_messages else datetime.now()
    days_span = max(1, (last_time - first_time).days + 1)
    unique_active_days = len(set(m["dateDay"] for m in parsed_messages))

    stats = {
        "totalMessages": len(parsed_messages),
        "partnerUid": partner_uid,
        "selfUid": self_uid,
        "users": user_counts,
        "firstTime": first_time.strftime("%Y-%m-%d %H:%M"),
        "lastTime": last_time.strftime("%Y-%m-%d %H:%M"),
        "daysSpan": days_span,
        "activeDays": unique_active_days,
        "totalSongs": len(all_songs),
        "totalPics": len(all_pics),
        "totalTogether": len(together_sessions),
        "totalQuotes": sum(1 for m in parsed_messages if m["quote"]),
        "hoursDistribution": hours_distribution,
        "songs": all_songs,
        "pics": all_pics,
        "togetherSessions": together_sessions,
    }

    return parsed_messages, stats


def download_media_assets(output_dir: Path, messages: list[dict[str, Any]], stats: dict[str, Any]) -> None:
    media_dir = output_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    urls_to_download: dict[str, Path] = {}

    # Avatars
    for uid, u in stats["users"].items():
        if u.get("avatarUrl"):
            urls_to_download[u["avatarUrl"]] = media_dir / f"avatar_{uid}.jpg"

    # Images
    for idx, p in enumerate(stats["pics"]):
        if p.get("url"):
            urls_to_download[p["url"]] = media_dir / f"img_{idx+1}_{p.get('msgId')}.jpg"

    # Music / Playlist / Album covers & invite covers
    for m in messages:
        if m["type"] == "song":
            pic = m["data"].get("picUrl")
            sid = m["data"].get("songId")
            if pic and sid:
                urls_to_download[pic] = media_dir / f"song_{sid}.jpg"
        elif m["type"] == "together":
            cover = m["data"].get("cover")
            if cover and m.get("id"):
                urls_to_download[cover] = media_dir / f"invite_{m['id']}.jpg"

    print(f"正在下载媒体资源（共 {len(urls_to_download)} 项）至 {media_dir} ...")

    def fetch(item: tuple[str, Path]):
        url, path = item
        if path.exists() and path.stat().st_size > 0:
            return
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read()
                path.write_bytes(content)
        except Exception:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(fetch, urls_to_download.items()))

    # Rewrite paths to relative local paths if downloaded
    for uid, u in stats["users"].items():
        local_p = media_dir / f"avatar_{uid}.jpg"
        if local_p.exists():
            u["avatarUrl"] = f"media/avatar_{uid}.jpg"

    for s in stats["songs"]:
        sid = s.get("songId")
        local_p = media_dir / f"song_{sid}.jpg"
        if sid and local_p.exists():
            s["picUrl"] = f"media/{local_p.name}"

    for m in messages:
        if m["senderId"] in stats["users"] and (media_dir / f"avatar_{m['senderId']}.jpg").exists():
            m["senderAvatar"] = f"media/avatar_{m['senderId']}.jpg"

        if m["type"] == "image":
            for idx, p in enumerate(stats["pics"]):
                if p.get("msgId") == m["id"]:
                    local_p = media_dir / f"img_{idx+1}_{p.get('msgId')}.jpg"
                    if local_p.exists():
                        m["data"]["url"] = f"media/{local_p.name}"
                        p["url"] = f"media/{local_p.name}"

        elif m["type"] == "song":
            sid = m["data"].get("songId")
            local_p = media_dir / f"song_{sid}.jpg"
            if sid and local_p.exists():
                m["data"]["picUrl"] = f"media/{local_p.name}"

        elif m["type"] == "together" and m.get("id"):
            local_p = media_dir / f"invite_{m['id']}.jpg"
            if local_p.exists():
                m["data"]["cover"] = f"media/{local_p.name}"

    print("媒体资源本地化完成。")


def build_modern_html(messages: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    partner = stats["users"].get(stats["partnerUid"]) or {
        "nickname": "对方",
        "avatarUrl": "",
        "count": 0,
    }
    myself = stats["users"].get(stats["selfUid"]) or {
        "nickname": "我",
        "avatarUrl": "",
        "count": 0,
    }

    raw_json_data = json.dumps({
        "messages": messages,
        "stats": stats,
    }, ensure_ascii=False).replace("</", "<\\/")

    total_m = max(stats["totalMessages"], 1)
    self_pct = round(myself["count"] / total_m * 100, 1)
    partner_pct = round(partner["count"] / total_m * 100, 1)

    tpl = '<!DOCTYPE html>\n<html lang="zh-CN" data-theme="dark" data-accent="blue">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">\n<title>Messages · __PARTNER_NAME__</title>\n<style>\n:root {\n  --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Inter", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;\n  --font-mono: "SF Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;\n\n  /* Dark Mode (macOS Sequoia / Linear inspired) */\n  --bg-canvas: #090a0c;\n  --bg-header: rgba(16, 17, 20, 0.78);\n  --bg-elevated: rgba(24, 25, 30, 0.88);\n  --bg-card: #141519;\n  --bg-card-hover: #1b1d22;\n  --bg-subtle: rgba(255, 255, 255, 0.04);\n  --bg-input: rgba(255, 255, 255, 0.06);\n\n  --bubble-in: #1c1d22;\n  --bubble-in-border: rgba(255, 255, 255, 0.06);\n  --bubble-in-text: #f2f3f5;\n\n  --text-primary: #f5f5f7;\n  --text-secondary: rgba(235, 235, 245, 0.62);\n  --text-tertiary: rgba(235, 235, 245, 0.36);\n\n  --border-subtle: rgba(255, 255, 255, 0.07);\n  --border-strong: rgba(255, 255, 255, 0.14);\n  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.24);\n  --shadow-lg: 0 16px 40px -8px rgba(0, 0, 0, 0.55);\n}\n\n[data-theme="light"] {\n  --bg-canvas: #f5f5f7;\n  --bg-header: rgba(245, 245, 247, 0.82);\n  --bg-elevated: rgba(255, 255, 255, 0.92);\n  --bg-card: #ffffff;\n  --bg-card-hover: #f9f9fb;\n  --bg-subtle: rgba(0, 0, 0, 0.035);\n  --bg-input: rgba(0, 0, 0, 0.05);\n\n  --bubble-in: #e9e9eb;\n  --bubble-in-border: rgba(0, 0, 0, 0.03);\n  --bubble-in-text: #1d1d1f;\n\n  --text-primary: #1d1d1f;\n  --text-secondary: #6e6e73;\n  --text-tertiary: #86868b;\n\n  --border-subtle: rgba(0, 0, 0, 0.07);\n  --border-strong: rgba(0, 0, 0, 0.14);\n  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04);\n  --shadow-lg: 0 16px 40px -8px rgba(0, 0, 0, 0.12);\n}\n\n/* Accent Palettes: iMessage Blue vs NetEase Crimson */\n[data-accent="blue"] {\n  --accent: #007aff;\n  --accent-soft: rgba(0, 122, 255, 0.14);\n  --bubble-out: linear-gradient(180deg, #1b86ff 0%, #0066eb 100%);\n  --bubble-out-shadow: 0 2px 10px rgba(0, 122, 255, 0.22);\n}\n\n[data-accent="crimson"] {\n  --accent: #e02838;\n  --accent-soft: rgba(224, 40, 56, 0.14);\n  --bubble-out: linear-gradient(180deg, #e53242 0%, #c81d2c 100%);\n  --bubble-out-shadow: 0 2px 10px rgba(224, 40, 56, 0.22);\n}\n\n* {\n  box-sizing: border-box;\n  margin: 0;\n  padding: 0;\n  -webkit-font-smoothing: antialiased;\n  -moz-osx-font-smoothing: grayscale;\n}\n\nbody {\n  font-family: var(--font-sans);\n  background: var(--bg-canvas);\n  color: var(--text-primary);\n  height: 100vh;\n  display: flex;\n  flex-direction: column;\n  overflow: hidden;\n  letter-spacing: -0.008em;\n}\n\n/* Custom Subtle Scrollbar */\n::-webkit-scrollbar { width: 6px; height: 6px; }\n::-webkit-scrollbar-track { background: transparent; }\n::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 3px; }\n\n/* Top macOS Navigation Bar */\n.topbar {\n  height: 56px;\n  background: var(--bg-header);\n  backdrop-filter: blur(24px) saturate(180%);\n  -webkit-backdrop-filter: blur(24px) saturate(180%);\n  border-bottom: 1px solid var(--border-subtle);\n  display: flex;\n  align-items: center;\n  justify-content: space-between;\n  padding: 0 20px;\n  z-index: 100;\n  flex-shrink: 0;\n}\n\n.contact-meta {\n  display: flex;\n  align-items: center;\n  gap: 11px;\n  min-width: 0;\n}\n\n.contact-avatar {\n  width: 34px;\n  height: 34px;\n  border-radius: 50%;\n  object-fit: cover;\n  border: 1px solid var(--border-strong);\n  flex-shrink: 0;\n}\n\n.contact-info {\n  display: flex;\n  flex-direction: column;\n  min-width: 0;\n}\n\n.contact-name-row {\n  display: flex;\n  align-items: center;\n  gap: 8px;\n}\n\n.contact-name {\n  font-size: 14.5px;\n  font-weight: 600;\n  color: var(--text-primary);\n  white-space: nowrap;\n  overflow: hidden;\n  text-overflow: ellipsis;\n}\n\n.count-pill {\n  font-size: 11px;\n  font-weight: 500;\n  font-variant-numeric: tabular-nums;\n  padding: 1px 7px;\n  border-radius: 999px;\n  background: var(--bg-subtle);\n  border: 1px solid var(--border-subtle);\n  color: var(--text-secondary);\n}\n\n.contact-sub {\n  font-size: 11.5px;\n  color: var(--text-tertiary);\n  font-variant-numeric: tabular-nums;\n}\n\n/* Toolbar Controls */\n.toolbar {\n  display: flex;\n  align-items: center;\n  gap: 6px;\n}\n\n.search-box {\n  position: relative;\n  display: flex;\n  align-items: center;\n}\n\n.search-icon {\n  position: absolute;\n  left: 10px;\n  color: var(--text-tertiary);\n  pointer-events: none;\n  display: flex;\n}\n\n.search-input {\n  background: var(--bg-input);\n  border: 1px solid var(--border-subtle);\n  color: var(--text-primary);\n  border-radius: 8px;\n  padding: 5px 62px 5px 30px;\n  font-size: 12.5px;\n  width: 170px;\n  font-family: inherit;\n  transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);\n}\n\n.search-input:focus {\n  width: 230px;\n  border-color: var(--accent);\n  background: var(--bg-card);\n  outline: none;\n  box-shadow: 0 0 0 3px var(--accent-soft);\n}\n\n.search-input::placeholder {\n  color: var(--text-tertiary);\n}\n\n.search-nav {\n  position: absolute;\n  right: 4px;\n  display: flex;\n  align-items: center;\n  gap: 2px;\n}\n\n.search-counter {\n  font-size: 10.5px;\n  color: var(--text-tertiary);\n  font-variant-numeric: tabular-nums;\n  padding-right: 2px;\n}\n\n.icon-btn-sm {\n  background: transparent;\n  border: none;\n  color: var(--text-secondary);\n  width: 20px;\n  height: 20px;\n  border-radius: 4px;\n  display: inline-flex;\n  align-items: center;\n  justify-content: center;\n  cursor: pointer;\n}\n\n.icon-btn-sm:hover {\n  background: var(--bg-subtle);\n  color: var(--text-primary);\n}\n\n.divider-v {\n  width: 1px;\n  height: 18px;\n  background: var(--border-subtle);\n  margin: 0 4px;\n}\n\n.tool-btn {\n  background: transparent;\n  border: 1px solid transparent;\n  color: var(--text-secondary);\n  height: 30px;\n  padding: 0 10px;\n  border-radius: 7px;\n  font-size: 12.5px;\n  font-weight: 500;\n  font-family: inherit;\n  cursor: pointer;\n  display: inline-flex;\n  align-items: center;\n  gap: 6px;\n  transition: all 0.15s ease;\n  white-space: nowrap;\n}\n\n.tool-btn:hover {\n  background: var(--bg-subtle);\n  color: var(--text-primary);\n}\n\n.tool-btn.active {\n  background: var(--accent-soft);\n  color: var(--accent);\n}\n\n.tool-btn .num {\n  font-size: 11px;\n  color: var(--text-tertiary);\n  font-variant-numeric: tabular-nums;\n}\n\n.tool-btn-icon {\n  width: 30px;\n  padding: 0;\n  justify-content: center;\n}\n\n/* Main Stream */\n.chat-viewport {\n  flex: 1;\n  overflow-y: auto;\n  padding: 28px 20px 80px;\n  scroll-behavior: smooth;\n}\n\n.chat-stream {\n  max-width: 780px;\n  width: 100%;\n  margin: 0 auto;\n  display: flex;\n  flex-direction: column;\n}\n\n/* Filter Notice Banner */\n.filter-banner {\n  background: var(--bg-card);\n  border: 1px solid var(--border-strong);\n  border-radius: 10px;\n  padding: 9px 14px;\n  margin-bottom: 18px;\n  font-size: 12.5px;\n  color: var(--text-secondary);\n  display: flex;\n  align-items: center;\n  justify-content: space-between;\n}\n\n/* Time Divider (macOS iMessage Style) */\n.time-divider {\n  text-align: center;\n  margin: 22px 0 12px;\n  user-select: none;\n}\n\n.time-divider span {\n  font-size: 11px;\n  font-weight: 500;\n  color: var(--text-tertiary);\n  font-variant-numeric: tabular-nums;\n  letter-spacing: 0.01em;\n}\n\n/* Message Rows & Smart Grouping */\n.msg-row {\n  display: flex;\n  align-items: flex-end;\n  gap: 10px;\n  max-width: 78%;\n  margin-top: 14px;\n  position: relative;\n}\n\n.msg-row.grouped-prev {\n  margin-top: 3px;\n}\n\n.msg-row.left {\n  align-self: flex-start;\n}\n\n.msg-row.right {\n  align-self: flex-end;\n  flex-direction: row-reverse;\n}\n\n.msg-avatar {\n  width: 30px;\n  height: 30px;\n  border-radius: 50%;\n  object-fit: cover;\n  flex-shrink: 0;\n  border: 1px solid var(--border-subtle);\n}\n\n.msg-row.grouped-next .msg-avatar {\n  visibility: hidden;\n}\n\n.msg-stack {\n  display: flex;\n  flex-direction: column;\n  min-width: 0;\n}\n\n.msg-row.right .msg-stack {\n  align-items: flex-end;\n}\n\n.msg-sender-line {\n  font-size: 11px;\n  color: var(--text-tertiary);\n  margin: 0 0 4px 4px;\n  display: flex;\n  align-items: center;\n  gap: 6px;\n}\n\n.msg-row.right .msg-sender-line {\n  margin: 0 4px 4px 0;\n}\n\n.msg-row.grouped-prev .msg-sender-line {\n  display: none;\n}\n\n.msg-bubble-wrap {\n  display: flex;\n  align-items: center;\n  gap: 8px;\n}\n\n.msg-row.right .msg-bubble-wrap {\n  flex-direction: row-reverse;\n}\n\n.msg-hover-time {\n  font-size: 10.5px;\n  color: var(--text-tertiary);\n  font-variant-numeric: tabular-nums;\n  opacity: 0;\n  transition: opacity 0.15s ease;\n  user-select: none;\n  white-space: nowrap;\n}\n\n.msg-row:hover .msg-hover-time {\n  opacity: 1;\n}\n\n/* Bubbles */\n.msg-bubble {\n  padding: 9px 14px;\n  font-size: 14px;\n  line-height: 1.5;\n  word-break: break-word;\n  position: relative;\n  border-radius: 18px;\n  transition: transform 0.15s ease;\n}\n\n.msg-row.left .msg-bubble {\n  background: var(--bubble-in);\n  color: var(--bubble-in-text);\n  border: 1px solid var(--bubble-in-border);\n  border-bottom-left-radius: 5px;\n}\n\n.msg-row.left.grouped-next .msg-bubble {\n  border-bottom-left-radius: 18px;\n}\n\n.msg-row.left.grouped-prev .msg-bubble {\n  border-top-left-radius: 6px;\n}\n\n.msg-row.right .msg-bubble {\n  background: var(--bubble-out);\n  color: #ffffff;\n  box-shadow: var(--bubble-out-shadow), inset 0 1px 0 rgba(255, 255, 255, 0.16);\n  border-bottom-right-radius: 5px;\n}\n\n.msg-row.right.grouped-next .msg-bubble {\n  border-bottom-right-radius: 18px;\n}\n\n.msg-row.right.grouped-prev .msg-bubble {\n  border-top-right-radius: 6px;\n}\n\n/* Media / Rich Cards don\'t use standard text padding */\n.msg-bubble.is-card {\n  padding: 0;\n  background: transparent !important;\n  border: none !important;\n  box-shadow: none !important;\n}\n\n.msg-bubble a {\n  color: inherit;\n  text-decoration: underline;\n  text-underline-offset: 2px;\n}\n\n/* Quote Reply Bar */\n.quote-bar {\n  background: rgba(0, 0, 0, 0.16);\n  border-left: 2.5px solid rgba(255, 255, 255, 0.65);\n  border-radius: 6px;\n  padding: 5px 9px;\n  margin-bottom: 6px;\n  font-size: 12px;\n  cursor: pointer;\n  display: flex;\n  flex-direction: column;\n  gap: 1px;\n  transition: opacity 0.15s;\n}\n\n.quote-bar:hover { opacity: 0.85; }\n\n.msg-row.left .quote-bar {\n  background: var(--bg-subtle);\n  border-left-color: var(--accent);\n}\n\n.quote-author {\n  font-size: 11px;\n  font-weight: 600;\n  opacity: 0.9;\n}\n\n.quote-snippet {\n  font-size: 11.5px;\n  opacity: 0.75;\n  white-space: nowrap;\n  overflow: hidden;\n  text-overflow: ellipsis;\n  max-width: 260px;\n}\n\n/* Highlight Jump Flash */\n@keyframes pulseTarget {\n  0% { box-shadow: 0 0 0 0 var(--accent); transform: scale(1.02); }\n  50% { box-shadow: 0 0 0 6px transparent; transform: scale(1); }\n  100% { box-shadow: 0 0 0 0 transparent; }\n}\n.highlight-flash .msg-bubble {\n  animation: pulseTarget 1.4s cubic-bezier(0.16, 1, 0.3, 1);\n}\n\n/* Image Message */\n.media-image-card {\n  position: relative;\n  cursor: zoom-in;\n  overflow: hidden;\n  border-radius: 14px;\n  border: 1px solid var(--border-subtle);\n  max-width: 300px;\n  max-height: 380px;\n  background: var(--bg-card);\n}\n\n.media-image-card img {\n  display: block;\n  max-width: 100%;\n  max-height: 380px;\n  object-fit: contain;\n  transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);\n}\n\n.media-image-card:hover img {\n  transform: scale(1.015);\n}\n\n/* Apple Music Track Card */\n.track-card {\n  display: flex;\n  align-items: center;\n  gap: 12px;\n  background: var(--bg-card);\n  border: 1px solid var(--border-strong);\n  padding: 10px 12px;\n  border-radius: 14px;\n  width: 290px;\n  box-shadow: var(--shadow-sm);\n  color: var(--text-primary);\n  text-decoration: none !important;\n  transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);\n}\n\n.track-card:hover {\n  background: var(--bg-card-hover);\n  transform: translateY(-1px);\n  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.2);\n}\n\n.track-artwork-wrap {\n  position: relative;\n  width: 48px;\n  height: 48px;\n  flex-shrink: 0;\n}\n\n.track-artwork {\n  width: 48px;\n  height: 48px;\n  border-radius: 8px;\n  object-fit: cover;\n  border: 1px solid var(--border-subtle);\n}\n\n.track-meta {\n  flex: 1;\n  min-width: 0;\n  display: flex;\n  flex-direction: column;\n  gap: 2px;\n}\n\n.track-title {\n  font-weight: 600;\n  font-size: 13.5px;\n  color: var(--text-primary);\n  white-space: nowrap;\n  overflow: hidden;\n  text-overflow: ellipsis;\n}\n\n.track-artist {\n  font-size: 12px;\n  color: var(--text-secondary);\n  white-space: nowrap;\n  overflow: hidden;\n  text-overflow: ellipsis;\n}\n\n.track-tag {\n  font-size: 10.5px;\n  color: var(--text-tertiary);\n  margin-top: 1px;\n}\n\n.track-play-btn {\n  width: 30px;\n  height: 30px;\n  border-radius: 50%;\n  background: var(--accent-soft);\n  color: var(--accent);\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  flex-shrink: 0;\n  transition: all 0.15s;\n}\n\n.track-card:hover .track-play-btn {\n  background: var(--accent);\n  color: #fff;\n}\n\n/* Comment Card */\n.review-card {\n  background: var(--bg-card);\n  border: 1px solid var(--border-strong);\n  border-radius: 14px;\n  padding: 12px 14px;\n  width: 290px;\n  color: var(--text-primary);\n  box-shadow: var(--shadow-sm);\n}\n\n.review-quote {\n  font-size: 13px;\n  line-height: 1.55;\n  color: var(--text-primary);\n  margin-bottom: 8px;\n}\n\n.review-footer {\n  display: flex;\n  align-items: center;\n  justify-content: space-between;\n  font-size: 11px;\n  color: var(--text-tertiary);\n  border-top: 1px solid var(--border-subtle);\n  padding-top: 7px;\n}\n\n/* Listen Together Invite Card */\n.invite-card {\n  background: var(--bg-card);\n  border: 1px solid var(--border-strong);\n  border-radius: 14px;\n  padding: 11px 14px;\n  width: 280px;\n  display: flex;\n  align-items: center;\n  gap: 12px;\n  box-shadow: var(--shadow-sm);\n}\n\n.invite-icon-wrap {\n  width: 40px;\n  height: 40px;\n  border-radius: 10px;\n  background: var(--accent-soft);\n  color: var(--accent);\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  flex-shrink: 0;\n}\n\n.invite-cover {\n  width: 40px;\n  height: 40px;\n  border-radius: 10px;\n  object-fit: cover;\n  flex-shrink: 0;\n}\n\n.invite-body {\n  flex: 1;\n  min-width: 0;\n}\n\n.invite-title {\n  font-size: 13px;\n  font-weight: 600;\n  color: var(--text-primary);\n}\n\n.invite-desc {\n  font-size: 11.5px;\n  color: var(--text-secondary);\n  white-space: nowrap;\n  overflow: hidden;\n  text-overflow: ellipsis;\n  margin-top: 1px;\n}\n\n.system-pill {\n  font-size: 12px;\n  color: var(--text-tertiary);\n  background: var(--bg-subtle);\n  border: 1px solid var(--border-subtle);\n  padding: 6px 12px;\n  border-radius: 10px;\n}\n</style>\n<style>\n/* Modals & Side Sheets */\n.backdrop {\n  position: fixed;\n  inset: 0;\n  background: rgba(0, 0, 0, 0.55);\n  backdrop-filter: blur(8px);\n  -webkit-backdrop-filter: blur(8px);\n  z-index: 1000;\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  opacity: 0;\n  pointer-events: none;\n  transition: opacity 0.2s ease;\n}\n\n.backdrop.active {\n  opacity: 1;\n  pointer-events: auto;\n}\n\n.dialog-sheet {\n  background: var(--bg-elevated);\n  backdrop-filter: blur(30px) saturate(190%);\n  -webkit-backdrop-filter: blur(30px) saturate(190%);\n  border: 1px solid var(--border-strong);\n  border-radius: 18px;\n  width: 92%;\n  max-width: 600px;\n  max-height: 84vh;\n  overflow-y: auto;\n  padding: 24px;\n  box-shadow: var(--shadow-lg);\n  transform: scale(0.97) translateY(8px);\n  transition: transform 0.22s cubic-bezier(0.16, 1, 0.3, 1);\n}\n\n.backdrop.active .dialog-sheet {\n  transform: scale(1) translateY(0);\n}\n\n.sheet-header {\n  display: flex;\n  justify-content: space-between;\n  align-items: center;\n  margin-bottom: 20px;\n}\n\n.sheet-title {\n  font-size: 16px;\n  font-weight: 600;\n  letter-spacing: -0.01em;\n}\n\n.close-circle {\n  width: 28px;\n  height: 28px;\n  border-radius: 50%;\n  background: var(--bg-subtle);\n  border: 1px solid var(--border-subtle);\n  color: var(--text-secondary);\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  cursor: pointer;\n  transition: all 0.15s;\n}\n\n.close-circle:hover {\n  background: var(--bg-input);\n  color: var(--text-primary);\n}\n\n/* Side Inspector Drawer */\n.inspector-drawer {\n  position: fixed;\n  top: 56px;\n  right: -420px;\n  width: 390px;\n  max-width: 100vw;\n  height: calc(100vh - 56px);\n  background: var(--bg-elevated);\n  backdrop-filter: blur(30px) saturate(190%);\n  -webkit-backdrop-filter: blur(30px) saturate(190%);\n  border-left: 1px solid var(--border-subtle);\n  box-shadow: var(--shadow-lg);\n  z-index: 950;\n  transition: right 0.28s cubic-bezier(0.16, 1, 0.3, 1);\n  display: flex;\n  flex-direction: column;\n}\n\n.inspector-drawer.open {\n  right: 0;\n}\n\n.drawer-top {\n  padding: 14px 18px;\n  border-bottom: 1px solid var(--border-subtle);\n  display: flex;\n  justify-content: space-between;\n  align-items: center;\n}\n\n.drawer-body {\n  flex: 1;\n  overflow-y: auto;\n  padding: 16px;\n}\n\n/* Bento Stats Grid */\n.bento-grid {\n  display: grid;\n  grid-template-columns: repeat(3, 1fr);\n  gap: 10px;\n  margin-bottom: 18px;\n}\n\n.bento-cell {\n  background: var(--bg-card);\n  border: 1px solid var(--border-subtle);\n  border-radius: 12px;\n  padding: 14px 12px;\n  display: flex;\n  flex-direction: column;\n  gap: 4px;\n}\n\n.bento-label {\n  font-size: 11.5px;\n  color: var(--text-tertiary);\n}\n\n.bento-val {\n  font-size: 22px;\n  font-weight: 600;\n  color: var(--text-primary);\n  font-variant-numeric: tabular-nums;\n  letter-spacing: -0.02em;\n}\n\n.bento-unit {\n  font-size: 12px;\n  font-weight: 400;\n  color: var(--text-secondary);\n  margin-left: 2px;\n}\n\n.ratio-box {\n  background: var(--bg-card);\n  border: 1px solid var(--border-subtle);\n  border-radius: 12px;\n  padding: 14px 16px;\n  margin-bottom: 16px;\n}\n\n.ratio-header {\n  display: flex;\n  justify-content: space-between;\n  font-size: 12px;\n  color: var(--text-secondary);\n  margin-bottom: 8px;\n  font-variant-numeric: tabular-nums;\n}\n\n.ratio-track {\n  height: 6px;\n  border-radius: 3px;\n  background: var(--bg-input);\n  overflow: hidden;\n  display: flex;\n}\n\n.ratio-bar {\n  height: 100%;\n  background: var(--accent);\n  border-radius: 3px;\n}\n\n.chart-card {\n  background: var(--bg-card);\n  border: 1px solid var(--border-subtle);\n  border-radius: 12px;\n  padding: 14px 16px;\n}\n\n.chart-caption {\n  font-size: 12px;\n  color: var(--text-secondary);\n  margin-bottom: 12px;\n}\n\n.hours-bars {\n  display: flex;\n  align-items: flex-end;\n  gap: 4px;\n  height: 76px;\n  padding-top: 6px;\n}\n\n.hour-col {\n  flex: 1;\n  height: 100%;\n  display: flex;\n  align-items: flex-end;\n}\n\n.hour-fill {\n  width: 100%;\n  background: var(--accent);\n  opacity: 0.65;\n  border-radius: 2px;\n  min-height: 2px;\n  transition: opacity 0.15s;\n}\n\n.hour-col:hover .hour-fill {\n  opacity: 1;\n}\n\n.hour-axis {\n  display: flex;\n  justify-content: space-between;\n  font-size: 10px;\n  color: var(--text-tertiary);\n  margin-top: 6px;\n  font-variant-numeric: tabular-nums;\n}\n\n/* Photo Grid & Drawer Lists */\n.media-grid {\n  display: grid;\n  grid-template-columns: repeat(3, 1fr);\n  gap: 6px;\n}\n\n.media-cell {\n  aspect-ratio: 1;\n  border-radius: 8px;\n  overflow: hidden;\n  cursor: pointer;\n  background: var(--bg-card);\n  border: 1px solid var(--border-subtle);\n}\n\n.media-cell img {\n  width: 100%;\n  height: 100%;\n  object-fit: cover;\n  transition: transform 0.2s ease;\n}\n\n.media-cell:hover img {\n  transform: scale(1.04);\n}\n\n.drawer-list {\n  display: flex;\n  flex-direction: column;\n  gap: 8px;\n}\n\n.drawer-item {\n  display: flex;\n  align-items: center;\n  gap: 10px;\n  padding: 9px 10px;\n  border-radius: 10px;\n  background: var(--bg-card);\n  border: 1px solid var(--border-subtle);\n  text-decoration: none;\n  color: var(--text-primary);\n  transition: background 0.15s;\n}\n\n.drawer-item:hover {\n  background: var(--bg-card-hover);\n}\n\n.drawer-item-cover {\n  width: 40px;\n  height: 40px;\n  border-radius: 6px;\n  object-fit: cover;\n  flex-shrink: 0;\n}\n\n.session-item {\n  background: var(--bg-card);\n  border: 1px solid var(--border-subtle);\n  border-radius: 12px;\n  padding: 12px 14px;\n  margin-bottom: 8px;\n}\n\n.session-item-top {\n  display: flex;\n  justify-content: space-between;\n  align-items: center;\n  margin-bottom: 6px;\n}\n\n.session-item-title {\n  font-size: 13px;\n  font-weight: 600;\n  color: var(--text-primary);\n}\n\n.session-meta-row {\n  font-size: 11.5px;\n  color: var(--text-tertiary);\n  display: flex;\n  gap: 10px;\n  margin-bottom: 8px;\n  font-variant-numeric: tabular-nums;\n}\n\n/* Lightbox */\n.lightbox {\n  position: fixed;\n  inset: 0;\n  background: rgba(0, 0, 0, 0.9);\n  backdrop-filter: blur(16px);\n  -webkit-backdrop-filter: blur(16px);\n  z-index: 2000;\n  display: none;\n  align-items: center;\n  justify-content: center;\n}\n\n.lightbox.active { display: flex; }\n\n.lightbox-img {\n  max-width: 90vw;\n  max-height: 84vh;\n  object-fit: contain;\n  border-radius: 10px;\n  transition: transform 0.2s ease;\n}\n\n.lightbox-dock {\n  position: absolute;\n  bottom: 28px;\n  background: rgba(28, 28, 32, 0.85);\n  backdrop-filter: blur(20px);\n  border: 1px solid rgba(255, 255, 255, 0.12);\n  padding: 6px 10px;\n  border-radius: 999px;\n  display: flex;\n  gap: 6px;\n}\n\n@media (max-width: 768px) {\n  .topbar { padding: 0 12px; }\n  .contact-sub { display: none; }\n  .search-input { width: 120px; }\n  .search-input:focus { width: 160px; }\n  .tool-btn span:not(.num) { display: none; }\n  .msg-row { max-width: 88%; }\n}\n</style>\n</head>\n<body>\n\n<header class="topbar">\n  <div class="contact-meta">\n    <img src="__PARTNER_AVATAR__" class="contact-avatar" alt="" onerror="this.src=\'data:image/svg+xml;utf8,<svg xmlns=\\\'http://www.w3.org/2000/svg\\\' width=\\\'36\\\' height=\\\'36\\\'><rect width=\\\'36\\\' height=\\\'36\\\' fill=\\\'%23333\\\'/></svg>\'">\n    <div class="contact-info">\n      <div class="contact-name-row">\n        <span class="contact-name">__PARTNER_NAME__</span>\n        <span class="count-pill">__TOTAL_MESSAGES__</span>\n      </div>\n      <div class="contact-sub">__FIRST_TIME__ — __LAST_TIME__ · __DAYS_SPAN__d</div>\n    </div>\n  </div>\n\n  <div class="toolbar">\n    <div class="search-box">\n      <span class="search-icon">\n        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>\n      </span>\n      <input type="text" id="searchInput" class="search-input" placeholder="搜索对话..." oninput="handleSearch(this.value)">\n      <div class="search-nav">\n        <span id="searchCounter" class="search-counter"></span>\n        <button class="icon-btn-sm" onclick="navSearch(-1)" title="上一处">\n          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m18 15-6-6-6 6"/></svg>\n        </button>\n        <button class="icon-btn-sm" onclick="navSearch(1)" title="下一处">\n          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>\n        </button>\n      </div>\n    </div>\n\n    <div class="divider-v"></div>\n\n    <button class="tool-btn" onclick="openModal(\'statsModal\')" title="统计概览">\n      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>\n      <span>概览</span>\n    </button>\n\n    <button class="tool-btn" id="btnDrawerMusic" onclick="toggleDrawer(\'musicDrawer\')" title="分享曲目">\n      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>\n      <span>曲目</span>\n      <span class="num">__TOTAL_SONGS__</span>\n    </button>\n\n    <button class="tool-btn" id="btnDrawerPhoto" onclick="toggleDrawer(\'photoDrawer\')" title="媒体图库">\n      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>\n      <span>图库</span>\n      <span class="num">__TOTAL_PICS__</span>\n    </button>\n\n    <button class="tool-btn" id="btnDrawerTogether" onclick="toggleDrawer(\'togetherDrawer\')" title="共听记录">\n      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a9 9 0 0 1 18 0v7a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3"/></svg>\n      <span>共听</span>\n      <span class="num">__TOTAL_TOGETHER__</span>\n    </button>\n\n    <div class="divider-v"></div>\n\n    <button class="tool-btn tool-btn-icon" onclick="toggleAccent()" title="切换气泡主题色 (iMessage 蓝 / 网易红)">\n      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18Z" fill="currentColor"/></svg>\n    </button>\n\n    <button class="tool-btn tool-btn-icon" onclick="swapPerspective()" title="切换左右视角">\n      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m16 3 4 4-4 4"/><path d="M20 7H4"/><path d="m8 21-4-4 4-4"/><path d="M4 17h16"/></svg>\n    </button>\n\n    <button class="tool-btn tool-btn-icon" onclick="toggleTheme()" id="themeBtn" title="切换深浅外观">\n      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>\n    </button>\n  </div>\n</header>\n\n<main class="chat-viewport" id="chatViewport">\n  <div class="chat-stream" id="chatContainer"></div>\n</main>\n\n<!-- Image Lightbox -->\n<div class="lightbox" id="lightbox" onclick="closeLightbox()">\n  <img id="lightboxImg" class="lightbox-img" src="" alt="">\n  <div class="lightbox-dock" onclick="event.stopPropagation()">\n    <button class="tool-btn" style="color:#fff;" onclick="zoomLightbox(0.25)">放大</button>\n    <button class="tool-btn" style="color:#fff;" onclick="zoomLightbox(-0.25)">缩小</button>\n    <button class="tool-btn" style="color:#fff;" onclick="downloadCurrentImg()">原图</button>\n    <button class="tool-btn" style="color:rgba(255,255,255,0.6);" onclick="closeLightbox()">关闭</button>\n  </div>\n</div>\n\n<!-- Overview Modal -->\n<div class="backdrop" id="statsModal" onclick="closeModal(\'statsModal\', event)">\n  <div class="dialog-sheet" onclick="event.stopPropagation()">\n    <div class="sheet-header">\n      <div class="sheet-title">对话统计概览</div>\n      <button class="close-circle" onclick="closeModal(\'statsModal\')">\n        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>\n      </button>\n    </div>\n\n    <div class="bento-grid">\n      <div class="bento-cell">\n        <span class="bento-label">记录跨度</span>\n        <span class="bento-val">__DAYS_SPAN__<span class="bento-unit">天</span></span>\n      </div>\n      <div class="bento-cell">\n        <span class="bento-label">活跃天数</span>\n        <span class="bento-val">__ACTIVE_DAYS__<span class="bento-unit">天</span></span>\n      </div>\n      <div class="bento-cell">\n        <span class="bento-label">消息总数</span>\n        <span class="bento-val">__TOTAL_MESSAGES__<span class="bento-unit">条</span></span>\n      </div>\n      <div class="bento-cell">\n        <span class="bento-label">分享曲目</span>\n        <span class="bento-val">__TOTAL_SONGS__<span class="bento-unit">首</span></span>\n      </div>\n      <div class="bento-cell">\n        <span class="bento-label">图像文件</span>\n        <span class="bento-val">__TOTAL_PICS__<span class="bento-unit">张</span></span>\n      </div>\n      <div class="bento-cell">\n        <span class="bento-label">引用回复</span>\n        <span class="bento-val">__TOTAL_QUOTES__<span class="bento-unit">次</span></span>\n      </div>\n    </div>\n\n    <div class="ratio-box">\n      <div class="ratio-header">\n        <span>__SELF_NAME__ · __SELF_COUNT__ (__SELF_PCT__%)</span>\n        <span>__PARTNER_NAME__ · __PARTNER_COUNT__ (__PARTNER_PCT__%)</span>\n      </div>\n      <div class="ratio-track">\n        <div class="ratio-bar" style="width: __SELF_PCT__%;"></div>\n      </div>\n    </div>\n\n    <div class="chart-card">\n      <div class="chart-caption">24 小时消息分布</div>\n      <div class="hours-bars" id="hoursChart"></div>\n      <div class="hour-axis">\n        <span>00:00</span>\n        <span>06:00</span>\n        <span>12:00</span>\n        <span>18:00</span>\n        <span>23:00</span>\n      </div>\n    </div>\n  </div>\n</div>\n\n<!-- Side Drawers -->\n<aside class="inspector-drawer" id="musicDrawer">\n  <div class="drawer-top">\n    <span class="sheet-title">分享曲目 (__TOTAL_SONGS__)</span>\n    <div style="display:flex; gap:6px; align-items:center;">\n      <button class="tool-btn" style="height:26px; font-size:11.5px;" onclick="copyAllSongNames()">复制列表</button>\n      <button class="close-circle" onclick="closeAllDrawers()">\n        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>\n      </button>\n    </div>\n  </div>\n  <div class="drawer-body">\n    <div class="drawer-list" id="musicDrawerList"></div>\n  </div>\n</aside>\n\n<aside class="inspector-drawer" id="photoDrawer">\n  <div class="drawer-top">\n    <span class="sheet-title">媒体图库 (__TOTAL_PICS__)</span>\n    <button class="close-circle" onclick="closeAllDrawers()">\n      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>\n    </button>\n  </div>\n  <div class="drawer-body">\n    <div class="media-grid" id="photoGrid"></div>\n  </div>\n</aside>\n\n<aside class="inspector-drawer" id="togetherDrawer">\n  <div class="drawer-top">\n    <span class="sheet-title">共听时段 (__TOTAL_TOGETHER__)</span>\n    <button class="close-circle" onclick="closeAllDrawers()">\n      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>\n    </button>\n  </div>\n  <div class="drawer-body" id="togetherSessionList"></div>\n</aside>\n<script>\nconst DATA = __RAW_JSON_DATA__;\nlet currentTheme = \'dark\';\nlet currentAccent = \'blue\';\nlet isPerspectiveSwapped = false;\nlet searchMatches = [];\nlet searchIndex = -1;\nlet currentLightboxUrl = \'\';\nlet currentZoom = 1;\nlet activeFilterSessionId = null;\n\nfunction toggleTheme() {\n  currentTheme = currentTheme === \'dark\' ? \'light\' : \'dark\';\n  document.documentElement.setAttribute(\'data-theme\', currentTheme);\n}\n\nfunction toggleAccent() {\n  currentAccent = currentAccent === \'blue\' ? \'crimson\' : \'blue\';\n  document.documentElement.setAttribute(\'data-accent\', currentAccent);\n}\n\nfunction swapPerspective() {\n  isPerspectiveSwapped = !isPerspectiveSwapped;\n  renderMessages();\n}\n\nfunction initHoursChart() {\n  const container = document.getElementById(\'hoursChart\');\n  const counts = DATA.stats.hoursDistribution;\n  const maxVal = Math.max(...counts, 1);\n  container.innerHTML = counts.map((cnt, h) => {\n    const pct = Math.round((cnt / maxVal) * 100);\n    return `<div class="hour-col" title="${String(h).padStart(2,\'0\')}:00 · ${cnt} 条">\n      <div class="hour-fill" style="height:${Math.max(pct, 4)}%"></div>\n    </div>`;\n  }).join(\'\');\n}\n\nfunction initDrawers() {\n  const togetherList = document.getElementById(\'togetherSessionList\');\n  togetherList.innerHTML = DATA.stats.togetherSessions.map(s => `\n    <div class="session-item">\n      <div class="session-item-top">\n        <span class="session-item-title">Session #${s.sessionId}</span>\n        <span class="count-pill">${s.msgCount} 条</span>\n      </div>\n      <div class="session-meta-row">\n        <span>${s.startTime.slice(0, 16)}</span>\n        <span>${s.durationMin} min</span>\n        <span>${escapeHtml(s.inviter)}</span>\n      </div>\n      ${s.songs.length ? `<div style="font-size:11.5px; color:var(--text-secondary); margin-bottom:8px;">${escapeHtml(s.songs.join(\' / \'))}</div>` : \'\'}\n      <div style="display:flex; gap:6px;">\n        <button class="tool-btn" style="height:26px; font-size:11.5px; background:var(--bg-subtle);" onclick="jumpToTogetherSession(\'${s.startMsgId}\')">定位</button>\n        <button class="tool-btn ${activeFilterSessionId === s.sessionId ? \'active\' : \'\'}" style="height:26px; font-size:11.5px; background:var(--bg-subtle);" onclick="filterTogetherSession(${s.sessionId})">\n          ${activeFilterSessionId === s.sessionId ? \'取消筛选\' : \'仅看此段\'}\n        </button>\n      </div>\n    </div>\n  `).join(\'\');\n\n  const musicList = document.getElementById(\'musicDrawerList\');\n  musicList.innerHTML = DATA.stats.songs.map(s => `\n    <a href="${s.link}" target="_blank" class="drawer-item">\n      <img src="${s.picUrl}" class="drawer-item-cover" onerror="this.src=\'data:image/svg+xml;utf8,<svg xmlns=\\\\\'http://www.w3.org/2000/svg\\\\\' width=\\\\\'40\\\\\' height=\\\\\'40\\\\\'><rect width=\\\\\'40\\\\\' height=\\\\\'40\\\\\' fill=\\\\\'%23333\\\\\'/></svg>\'">\n      <div style="flex:1; min-width:0;">\n        <div style="font-weight:600; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(s.name)}</div>\n        <div style="font-size:11.5px; color:var(--text-secondary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(s.artists)}</div>\n        <div style="font-size:10.5px; color:var(--text-tertiary); margin-top:1px; font-variant-numeric:tabular-nums;">${s.time} · ${escapeHtml(s.sender)}</div>\n      </div>\n    </a>\n  `).join(\'\');\n\n  const photoGrid = document.getElementById(\'photoGrid\');\n  photoGrid.innerHTML = DATA.stats.pics.map(p => `\n    <div class="media-cell" onclick="openLightbox(\'${p.url}\')">\n      <img src="${p.url}" loading="lazy" alt="">\n    </div>\n  `).join(\'\');\n}\n\nfunction jumpToTogetherSession(msgId) {\n  closeAllDrawers();\n  if (activeFilterSessionId !== null) {\n    activeFilterSessionId = null;\n    renderMessages();\n  }\n  setTimeout(() => scrollToMessage(msgId), 120);\n}\n\nfunction filterTogetherSession(sessionId) {\n  activeFilterSessionId = activeFilterSessionId === sessionId ? null : sessionId;\n  initDrawers();\n  renderMessages();\n  closeAllDrawers();\n}\n\nfunction copyAllSongNames() {\n  const text = DATA.stats.songs.map(s => `${s.name} - ${s.artists}`).join(\'\\n\');\n  navigator.clipboard.writeText(text);\n}\n\nfunction renderMessages() {\n  const container = document.getElementById(\'chatContainer\');\n  let htmlChunks = [];\n  let lastTime = 0;\n\n  const msgs = activeFilterSessionId === null\n    ? DATA.messages\n    : DATA.messages.filter(m => m.togetherSessionId === activeFilterSessionId);\n\n  if (activeFilterSessionId !== null) {\n    htmlChunks.push(`\n      <div class="filter-banner">\n        <span>当前筛选：共听时段 #${activeFilterSessionId}（${msgs.length} 条记录）</span>\n        <button class="tool-btn" style="height:24px; font-size:11.5px;" onclick="filterTogetherSession(${activeFilterSessionId})">显示全部</button>\n      </div>\n    `);\n  }\n\n  for (let i = 0; i < msgs.length; i++) {\n    const m = msgs[i];\n    const prev = i > 0 ? msgs[i - 1] : null;\n    const next = i < msgs.length - 1 ? msgs[i + 1] : null;\n\n    const timeGapPrev = prev ? (m.timestamp - prev.timestamp) : Infinity;\n    const timeGapNext = next ? (next.timestamp - m.timestamp) : Infinity;\n\n    // Show time divider if > 10 minutes apart\n    if (m.timestamp - lastTime > 10 * 60 * 1000) {\n      htmlChunks.push(`<div class="time-divider"><span>${m.dateDay} ${m.dateTime}</span></div>`);\n      lastTime = m.timestamp;\n    }\n\n    const groupedPrev = prev && prev.senderId === m.senderId && timeGapPrev < 5 * 60 * 1000 && (m.timestamp - lastTime > 0);\n    const groupedNext = next && next.senderId === m.senderId && timeGapNext < 5 * 60 * 1000;\n\n    const isRight = isPerspectiveSwapped ? !m.isSelf : m.isSelf;\n    const sideClass = isRight ? \'right\' : \'left\';\n    const groupClasses = `${groupedPrev ? \'grouped-prev\' : \'\'} ${groupedNext ? \'grouped-next\' : \'\'}`.trim();\n\n    let quoteHtml = \'\';\n    if (m.quote) {\n      quoteHtml = `\n        <div class="quote-bar" onclick="scrollToMessage(\'${m.quote.refId}\')">\n          <div class="quote-author">${escapeHtml(m.quote.sender)}</div>\n          <div class="quote-snippet">${escapeHtml(m.quote.text)}</div>\n        </div>\n      `;\n    }\n\n    let bodyHtml = \'\';\n    let isStandaloneCard = false;\n\n    if (m.type === \'text\') {\n      bodyHtml = formatText(m.data.text);\n    } else if (m.type === \'image\') {\n      isStandaloneCard = !m.quote;\n      bodyHtml = `\n        <div class="media-image-card" onclick="openLightbox(\'${m.data.url}\')">\n          <img src="${m.data.url}" loading="lazy" alt="图片" onerror="this.style.minHeight=\'60px\'">\n        </div>\n      `;\n    } else if (m.type === \'song\') {\n      isStandaloneCard = !m.quote;\n      bodyHtml = `\n        <a href="${m.data.link}" target="_blank" class="track-card">\n          <div class="track-artwork-wrap">\n            <img src="${m.data.picUrl}" class="track-artwork" onerror="this.src=\'data:image/svg+xml;utf8,<svg xmlns=\\\\\'http://www.w3.org/2000/svg\\\\\' width=\\\\\'48\\\\\' height=\\\\\'48\\\\\'><rect width=\\\\\'48\\\\\' height=\\\\\'48\\\\\' fill=\\\\\'%23222\\\\\'/></svg>\'">\n          </div>\n          <div class="track-meta">\n            <div class="track-title">${escapeHtml(m.data.name)}</div>\n            <div class="track-artist">${escapeHtml(m.data.artists)}</div>\n            <div class="track-tag">${m.data.album ? escapeHtml(m.data.album) : \'网易云音乐\'}</div>\n          </div>\n          <div class="track-play-btn">\n            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"/></svg>\n          </div>\n        </a>\n      `;\n    } else if (m.type === \'comment\') {\n      isStandaloneCard = !m.quote;\n      bodyHtml = `\n        <div class="review-card">\n          <div class="review-quote">“${escapeHtml(m.data.content)}”</div>\n          <div class="review-footer">\n            <span>@${escapeHtml(m.data.author)}</span>\n            <span style="max-width:160px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(m.data.resourceName)}</span>\n          </div>\n        </div>\n      `;\n    } else if (m.type === \'together\') {\n      isStandaloneCard = !m.quote;\n      bodyHtml = `\n        <div class="invite-card">\n          ${m.data.cover\n            ? `<img src="${m.data.cover}" class="invite-cover" onerror="this.style.display=\'none\'">`\n            : `<div class="invite-icon-wrap"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a9 9 0 0 1 18 0v7a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3"/></svg></div>`\n          }\n          <div class="invite-body">\n            <div class="invite-title">${escapeHtml(m.data.title)}</div>\n            <div class="invite-desc">${escapeHtml(m.data.text)}</div>\n          </div>\n        </div>\n      `;\n    } else if (m.type === \'unsupported\') {\n      isStandaloneCard = true;\n      bodyHtml = `<div class="system-pill">${escapeHtml(m.data.notice)}</div>`;\n    }\n\n    htmlChunks.push(`\n      <div class="msg-row ${sideClass} ${groupClasses}" id="msg-${m.id}">\n        <img src="${m.senderAvatar}" class="msg-avatar" alt="" onerror="this.src=\'data:image/svg+xml;utf8,<svg xmlns=\\\\\'http://www.w3.org/2000/svg\\\\\' width=\\\\\'30\\\\\' height=\\\\\'30\\\\\'><rect width=\\\\\'30\\\\\' height=\\\\\'30\\\\\' fill=\\\\\'%23333\\\\\'/></svg>\'">\n        <div class="msg-stack">\n          <div class="msg-sender-line">\n            <span>${escapeHtml(m.senderName)}</span>\n            <span>${m.dateTime}</span>\n          </div>\n          <div class="msg-bubble-wrap">\n            <div class="msg-bubble ${isStandaloneCard ? \'is-card\' : \'\'}">\n              ${quoteHtml}\n              ${bodyHtml}\n            </div>\n            <span class="msg-hover-time">${m.dateStr.slice(11, 19)}</span>\n          </div>\n        </div>\n      </div>\n    `);\n  }\n\n  container.innerHTML = htmlChunks.join(\'\');\n}\n\nfunction escapeHtml(str) {\n  if (!str) return \'\';\n  return String(str)\n    .replace(/&/g, \'&amp;\')\n    .replace(/</g, \'&lt;\')\n    .replace(/>/g, \'&gt;\')\n    .replace(/"/g, \'&quot;\')\n    .replace(/\'/g, \'&#039;\');\n}\n\nfunction formatText(text) {\n  if (!text) return \'\';\n  let safe = escapeHtml(text);\n  safe = safe.replace(/(https?:\\/\\/[^\\s<]+)/g, \'<a href="$1" target="_blank">$1</a>\');\n  return safe.replace(/\\n/g, \'<br>\');\n}\n\nfunction scrollToMessage(id) {\n  if (!id) return;\n  const el = document.getElementById(`msg-${id}`);\n  if (el) {\n    el.scrollIntoView({ behavior: \'smooth\', block: \'center\' });\n    el.classList.remove(\'highlight-flash\');\n    void el.offsetWidth;\n    el.classList.add(\'highlight-flash\');\n  }\n}\n\nfunction handleSearch(keyword) {\n  keyword = keyword.trim().toLowerCase();\n  searchMatches = [];\n  searchIndex = -1;\n  const counter = document.getElementById(\'searchCounter\');\n\n  if (!keyword) {\n    counter.innerText = \'\';\n    return;\n  }\n\n  DATA.messages.forEach(m => {\n    let match = false;\n    if (m.type === \'text\' && m.data.text && m.data.text.toLowerCase().includes(keyword)) match = true;\n    if (m.type === \'song\' && (m.data.name.toLowerCase().includes(keyword) || m.data.artists.toLowerCase().includes(keyword))) match = true;\n    if (m.type === \'comment\' && m.data.content.toLowerCase().includes(keyword)) match = true;\n    if (match) searchMatches.push(m.id);\n  });\n\n  counter.innerText = searchMatches.length ? `0/${searchMatches.length}` : \'0\';\n  if (searchMatches.length > 0) navSearch(1);\n}\n\nfunction navSearch(direction) {\n  if (!searchMatches.length) return;\n  searchIndex += direction;\n  if (searchIndex >= searchMatches.length) searchIndex = 0;\n  if (searchIndex < 0) searchIndex = searchMatches.length - 1;\n  document.getElementById(\'searchCounter\').innerText = `${searchIndex + 1}/${searchMatches.length}`;\n  scrollToMessage(searchMatches[searchIndex]);\n}\n\nfunction openLightbox(url) {\n  currentLightboxUrl = url;\n  currentZoom = 1;\n  const lb = document.getElementById(\'lightbox\');\n  const img = document.getElementById(\'lightboxImg\');\n  img.src = url;\n  img.style.transform = `scale(${currentZoom})`;\n  lb.classList.add(\'active\');\n}\n\nfunction closeLightbox() {\n  document.getElementById(\'lightbox\').classList.remove(\'active\');\n}\n\nfunction zoomLightbox(delta) {\n  currentZoom = Math.max(0.4, Math.min(3.5, currentZoom + delta));\n  document.getElementById(\'lightboxImg\').style.transform = `scale(${currentZoom})`;\n}\n\nfunction downloadCurrentImg() {\n  const a = document.createElement(\'a\');\n  a.href = currentLightboxUrl;\n  a.download = \'image.jpg\';\n  a.target = \'_blank\';\n  document.body.appendChild(a);\n  a.click();\n  document.body.removeChild(a);\n}\n\nfunction openModal(id) {\n  document.getElementById(id).classList.add(\'active\');\n}\n\nfunction closeModal(id, evt) {\n  if (!evt || evt.target.id === id) {\n    document.getElementById(id).classList.remove(\'active\');\n  }\n}\n\nfunction closeAllDrawers() {\n  [\'musicDrawer\', \'photoDrawer\', \'togetherDrawer\'].forEach(d => {\n    document.getElementById(d).classList.remove(\'open\');\n  });\n  [\'btnDrawerMusic\', \'btnDrawerPhoto\', \'btnDrawerTogether\'].forEach(b => {\n    const el = document.getElementById(b);\n    if (el) el.classList.remove(\'active\');\n  });\n}\n\nfunction toggleDrawer(id) {\n  const el = document.getElementById(id);\n  const isOpen = el.classList.contains(\'open\');\n  closeAllDrawers();\n  if (!isOpen) {\n    el.classList.add(\'open\');\n    const mapBtn = {\n      musicDrawer: \'btnDrawerMusic\',\n      photoDrawer: \'btnDrawerPhoto\',\n      togetherDrawer: \'btnDrawerTogether\'\n    }[id];\n    if (mapBtn && document.getElementById(mapBtn)) {\n      document.getElementById(mapBtn).classList.add(\'active\');\n    }\n  }\n}\n\ndocument.addEventListener(\'keydown\', e => {\n  if (e.key === \'Escape\') {\n    closeLightbox();\n    closeModal(\'statsModal\');\n    closeAllDrawers();\n  }\n});\n\nwindow.addEventListener(\'DOMContentLoaded\', () => {\n  if (!DATA.stats.totalTogether) {\n    const btn = document.getElementById(\'btnDrawerTogether\');\n    if (btn) btn.style.display = \'none\';\n  }\n  renderMessages();\n  initHoursChart();\n  initDrawers();\n});\n</script>\n</body>\n</html>\n'
    replacements = {
        "__PARTNER_NAME__": html.escape(partner["nickname"]),
        "__PARTNER_AVATAR__": partner["avatarUrl"],
        "__SELF_NAME__": html.escape(myself["nickname"]),
        "__SELF_COUNT__": str(myself["count"]),
        "__PARTNER_COUNT__": str(partner["count"]),
        "__SELF_PCT__": str(self_pct),
        "__PARTNER_PCT__": str(partner_pct),
        "__TOTAL_MESSAGES__": str(stats["totalMessages"]),
        "__FIRST_TIME__": str(stats["firstTime"])[:10],
        "__LAST_TIME__": str(stats["lastTime"])[:10],
        "__DAYS_SPAN__": str(stats["daysSpan"]),
        "__ACTIVE_DAYS__": str(stats["activeDays"]),
        "__TOTAL_SONGS__": str(stats["totalSongs"]),
        "__TOTAL_PICS__": str(stats["totalPics"]),
        "__TOTAL_TOGETHER__": str(stats["totalTogether"]),
        "__TOTAL_QUOTES__": str(stats["totalQuotes"]),
        "__RAW_JSON_DATA__": raw_json_data,
    }
    pattern = re.compile("|".join(re.escape(k) for k in replacements))
    return pattern.sub(lambda m: replacements[m.group(0)], tpl)


def process_directory(dm_dir: Path, download_media: bool = False, quiet: bool = False) -> None:
    raw_file = dm_dir / "raw_messages.json"
    if not raw_file.exists():
        print(f"未找到原始消息文件: {raw_file}", file=sys.stderr)
        return

    with raw_file.open("r", encoding="utf-8") as f:
        records = json.load(f)

    # Extract target UID from directory name if present (e.g. netease_dm_123456789)
    match = re.search(r"netease_dm_(\d+)", dm_dir.name)
    target_uid = match.group(1) if match else None

    messages, stats = parse_raw_messages(records, target_uid=target_uid)

    if download_media:
        download_media_assets(dm_dir, messages, stats)

    html_file = dm_dir / "chat_history.html"
    modern_html = build_modern_html(messages, stats)
    html_file.write_text(modern_html, encoding="utf-8")

    if not quiet:
        print(f"已生成 HTML 归档: {html_file.resolve()}")
        print(
            f"统计: 消息 {stats['totalMessages']} 条 | "
            f"单曲 {stats['totalSongs']} 首 | "
            f"图片 {stats['totalPics']} 张 | "
            f"引用 {stats['totalQuotes']} 次"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="根据导出的 raw_messages.json 生成离线 HTML 聊天记录归档。"
    )
    parser.add_argument(
        "--dir",
        type=str,
        default="",
        help="指定 netease_dm_<UID> 目录路径（默认自动扫描当前工作目录）。",
    )
    parser.add_argument(
        "--download-media",
        action="store_true",
        help="将头像、聊天图片与封面下载至本地 media/ 目录，用于离线浏览。",
    )
    args = parser.parse_args()

    workdir = Path.cwd()
    if args.dir:
        target_dirs = [Path(args.dir).resolve()]
    else:
        target_dirs = [p for p in workdir.glob("netease_dm_*") if p.is_dir()]
        if not target_dirs and (workdir / "raw_messages.json").exists():
            target_dirs = [workdir]

    if not target_dirs:
        print("未找到 netease_dm_* 备份目录，请通过 --dir 指定路径。", file=sys.stderr)
        sys.exit(1)

    for d in target_dirs:
        process_directory(d, download_media=args.download_media)


if __name__ == "__main__":
    main()
