# 狗头军师助手

把开源恋爱军师 Skill「goutoujunshi」做成 Windows 常驻话术工具：**Ctrl+Alt+M 唤起 → 读剪贴板 / 贴聊天记录 → 生成一条能直接发出去的话**，并按对象保存档案、记忆、说话风格和效果反馈。

> 只处理你主动复制或导出的文本。不读取微信加密数据库、不 hook 微信进程、不自动发送消息。生成结果请自己确认后再发送。

## 能做什么

- **热键唤起**：`Ctrl+Alt+M` 唤起窗口，剪贴板里是聊天记录时直接进入生成流程。
- **聊天记录导入**：支持粘贴、txt/log 导入、文件夹增量同步；自动区分「我」和「对方」。
- **对象档案**：每个聊天对象独立保存关系、背景、关键事件、说话风格、回复效果。
- **话术生成**：基于本地技能内核、对象档案、历史话术和当前聊天记录，输出可直接发送的回复。
- **话术库**：把真正好用的话存下来，按对象检索、复制、删除。
- **填入聊天窗口**：复制话术后定位微信输入框并粘贴，只粘贴、不回车、不自动发送。
- **托盘常驻**：关闭窗口不退出，托盘菜单可重新打开或退出。
- **全局字号**：设置里 8 档字号，主界面标题、按钮、正文一起缩放。

## 快速开始

环境要求：Windows 10/11、Python 3.10+（安装时勾选 tcl/tk）。

```powershell
git clone https://github.com/Mine1178/goutoujunshi-assistant.git
cd goutoujunshi-assistant
python -m pip install -r requirements.txt
python tools\bootstrap.py
python main.py
```

也可以直接双击 **`启动.bat`**：脚本会寻找可用 Python、补装依赖、拉取技能、生成图标，然后启动。

首次启动后：

1. 打开 **设置**，填入任意 OpenAI 兼容接口的 `Base URL`、`API Key` 和模型名（默认是 DeepSeek）。
2. 填写你的微信昵称，用于区分聊天记录里谁在说话。
3. 需要导入历史记录时，把 txt/log 放进 `chat/`，在「导入聊天」里选择「用 chat」。
4. 微信里选中消息复制，按 `Ctrl+Alt+M`，生成后点「填入聊天窗口」，确认内容无误再自己按 Enter。

## 技能来源

界面和本地运行依赖上游技能目录 `skill/goutoujunshi`。为了尊重第三方仓库，本仓库**不复制技能正文**，由 `tools/bootstrap.py` 在首次运行时从下面地址拉取：

- <https://github.com/shengjidaguai-china/goutoujunshi>
- 上游许可证：MIT（Copyright © 2026 powerycy）

如果不想联网，也可以手动克隆到 `skill/goutoujunshi`，只要目录里有 `SKILL.md` 即可：

```powershell
git clone https://github.com/shengjidaguai-china/goutoujunshi.git skill/goutoujunshi
```

## 打包 Windows 安装包

```powershell
python -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File .\打包.ps1
```

需要安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php)。产物在 `dist/`：

- `dist/GoutouJunshi/GoutouJunshi.exe`：目录版，可直接运行
- `dist/GoutouJunshi_Setup_v2.5.exe`：安装包

开源仓库不带预编译 exe，也不带上游技能正文，打包脚本会自动准备。

## 目录结构

```text
.
├─ main.py                 # 入口、单实例、日志
├─ ui.py                   # 主界面、设置、导入、对象管理、话术库
├─ contacts.py             # 档案、记忆、风格、评分
├─ skill_engine.py         # 技能加载、知识路由、提示词组装
├─ llm_client.py           # OpenAI 兼容接口
├─ wechat_bridge.py        # 查找微信窗口并粘贴（不自动发送）
├─ wechat_import.py        # 聊天文本解析与对象识别
├─ store.py                # 本地配置与数据
├─ system_integration.py   # 托盘、开机自启
├─ tools/bootstrap.py      # 拉取技能、生成图标
├─ installer.iss           # Inno Setup 安装脚本
├─ 打包.ps1                # PyInstaller + Inno Setup
└─ assets/                 # 运行后生成图标；二维码不随仓库分发
```

## 隐私与数据

- 配置、档案、话术库、历史记录都保存在程序目录的 `data/` 下，不上传本项目仓库。
- 只有你主动点击「生成回复 / 追问 / 换一版」时，程序才会把当前拼好的上下文发送到你配置的模型接口。
- `data/`、`chat/`、`dist/`、构建日志和本地二维码都写在 `.gitignore`，不会被误提交。
- **不要在 Issue、截图或 Pull Request 里贴 API Key、聊天原文、对方姓名或二维码。**

## 已知边界

- 仅支持 Windows；微信窗口查找依赖微信桌面版，界面版本变化可能导致「填入聊天窗口」找不到窗口。
- 不读取微信数据库，也不监听微信消息；必须由你手动复制或导入。
- 生成结果依赖你配置的模型，可能出现事实错误、语气不合适或越界建议，请自行判断。
- 本项目与微信、DeepSeek 及其他模型服务商均无隶属关系。

## 参与贡献

欢迎 Issue 和 Pull Request。提交前请确认：

- 不包含真实聊天记录、联系人或个人信息；
- 不包含 API Key、Token、Cookie 等凭据；
- 新增依赖时同步更新 `requirements.txt`；
- Python 代码通过 `python -m py_compile`。

## License

本项目代码采用 **MIT License**，见 [LICENSE](LICENSE)。

第三方技能「goutoujunshi」按上游 MIT License 使用，版权归其原作者所有。
