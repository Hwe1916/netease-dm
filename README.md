# netease-dm

网易云音乐私信聊天记录本地备份与离线归档工具。

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![API: api-enhanced](https://img.shields.io/badge/api-NeteaseCloudMusicApiEnhanced-d92332.svg)](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)

## 简介

网易云音乐客户端未内置私信导出功能，历史会话中的外链图片可能随时间失效，单曲分享、评论卡片与引用回复也难以通过截图完整留存。

本项目配合本地部署的 [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced) 服务，将指定好友的历史私信完整导出至本地，并生成单文件离线交互网页（`chat_history.html`）、纯文本记录与原始 JSON 数据。

## 功能说明

- **历史记录分页导出与断点续传**：按消息时间戳向前逐页拉取历史私信；拉取过程实时写入增量文件（`partial_messages.jsonl`），中途中断后重新运行可自动续传。
- **富媒体卡片解析与本地化**：
  - 支持解析单曲、歌单、专辑分享卡片、评论分享卡片、图片与表情、共听邀请卡片以及引用回复跳转。
  - 支持通过 `--download-media` 将对话双方头像、聊天原图与音乐封面批量下载至本地 `media/` 目录，便于离线查阅。
- **离线归档阅读页面（`chat_history.html`）**：
  - 简洁的桌面端气泡对话布局，5 分钟内同一发送者的连续消息自动合并头像与昵称，悬停可查看精确到秒的发送时间。
  - 支持关键词全文检索与上下定位、分享曲目汇总（可一键复制曲目列表）、图片画廊预览及消息时段分布统计。
  - 支持深色 / 浅色模式切换、气泡配色切换（蓝 / 红）与左右视角翻转。

## 快速开始

### 1. 环境准备

需安装 Python 3.8+ 与 Node.js（用于在本地运行 API 服务）：

```bash
git clone https://github.com/Hwe1916/netease-dm.git
cd netease-dm
pip install -r requirements.txt
```

### 2. 启动本地 API 服务

在独立终端中克隆并启动 [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)（默认监听 `http://127.0.0.1:3000`）：

```bash
git clone https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced.git
cd api-enhanced
npm install
node app.js
```

### 3. 导出私信记录

运行备份脚本：

```bash
python backup_dm.py
```

如需在导出完成后一并下载所有头像、聊天图片与音乐封面至本地，可附带 `--download-media` 参数：

```bash
python backup_dm.py --download-media
```

按终端提示操作：
1. 输入目标好友的**数字 UID**（可在网易云音乐 App 打开对方个人主页 -> 分享 -> 复制链接，链接中 `id=` 后的数字即为 UID）。
2. 脚本会生成一张临时登录二维码并在浏览器打开，使用本人网易云音乐 App 扫码确认（临时图片在登录完成后自动删除）。
3. 导出结束后，打开生成的 `netease_dm_<UID>/chat_history.html` 即可浏览。

### 4. 单独重新生成网页或补下媒体资源（可选）

若本地已有导出的 `raw_messages.json`，可随时单独调用渲染脚本重新生成 HTML 或补充下载图片资源：

```bash
python generate_modern_html.py --dir netease_dm_<UID> --download-media
```

## 输出目录结构

```text
netease_dm_<UID>/
├── chat_history.html        # 离线交互式聊天记录页面
├── chat_history.txt         # 纯文本格式聊天记录
├── raw_messages.json        # 去重排序后的原始接口消息数据
├── partial_messages.jsonl   # 分页拉取过程中的增量缓存文件
└── media/                   # 本地化的头像、图片与封面（启用 --download-media 时生成）
```

## 补充说明

- **关于本地接口分页缓存**：`api-enhanced` 默认启用了 2 分钟的响应缓存，缓存键基于请求 URL 与 Cookie 计算，未包含 POST 请求体。由于私信历史接口通过 POST 请求体传入分页游标（`before`），直接翻页会重复命中第一页缓存。本脚本在请求本地服务时会自动附加递增查询参数以区分各页请求，无需修改 `api-enhanced` 源码配置。
- **部分特殊消息说明**：受限于接口返回内容，客户端专属消息（如语音条、部分动态表情或已撤回/删除内容）可能仅保留提示文本而无法还原媒体文件。

## 隐私与免责声明

1. **本地隐私安全**：所有请求仅在你的本机与本地 `api-enhanced` 实例之间传输；扫码获取的登录凭证仅驻留于当前进程内存，不写入磁盘，也不包含任何第三方统计或上传逻辑。仓库 `.gitignore` 已默认忽略 `netease_dm_*/`、`media/`、`raw_messages.json` 等个人数据文件，避免误提交。
2. **合理使用**：本工具仅供个人导出并备份**本人账号**下的私信记录，请勿用于高频请求、批量抓取或任何商业用途。脚本已内置翻页请求间隔，请勿移除限速逻辑。
3. **免责声明**：本项目为独立的个人开源工具，与网易云音乐官方无任何关联。请遵守平台服务协议与相关法律法规，因不当使用引发的任何账号风险或责任由使用者自行承担。

## 致谢与许可

- 接口服务支持：[NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)
- 开源许可：[MIT License](./LICENSE)
