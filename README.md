# B站扫码登录 · Cookie 工具

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

一键扫码获取 B站 Cookie，支持本地生成二维码、自动检测端口、智能轮询。

## 功能

- 🔑 **扫码登录** — 调用 B站 passport 接口生成二维码，APP 扫码即登
- 🖼️ **本地生成二维码** — 无需依赖外部 API，离线可用
- 💾 **Cookie 持久化** — 登录成功后自动保存到 `cookie.json`
- 📋 **一键注入** — 复制代码粘贴到 F12 控制台即可注入 Cookie
- ✅ **有效性检测** — 验证 Cookie 是否过期，显示用户信息
- 🔌 **端口自动检测** — 8888 被占用自动顺延
- ⏱ **智能轮询** — 登录中 2 秒/次，登录后 30 秒/次

## 使用方法

### 方式一：直接运行源代码

```bash
pip install qrcode[pil]
python main.py
```

然后浏览器访问 http://127.0.0.1:8888

### 方式二：运行编译好的 exe

双击 `B站登录工具.exe` 即可，无需安装 Python。

> 也可通过 `--port` 指定端口：`B站登录工具.exe --port 9999`

## 项目结构

```
bilibili cookie/
├── main.py          # Python 后端
├── index.html       # Web 前端界面
├── cookie.json      # 登录凭证（不上传 Git）
└── .gitignore
```

## 技术栈

- **后端**：Python + http.server（标准库）
- **前端**：纯 HTML/CSS/JS（无框架）
- **二维码**：qrcode + Pillow 本地生成
