# 开发与发布

## 目录

```text
start.cmd / run.ps1     Windows 一键启动
web.py                 本机网页与私有代理入口
widget.py              Tkinter 置顶盯盘窗
main.py                命令行入口
remote.py / remote.cmd  可选 Tailscale 连接向导
stock_alert/           行情源、检测、分析、资讯、存储及接口
web/                   原生 HTML / CSS / JavaScript 前端
tests/                 Python 与前端回归测试
docs/                  使用与技术文档
tools/                 发布检查与打包工具
config.example.json    可提交的示例配置
```

日常运行依赖 Python 3.10+、requests、tzdata；悬浮窗依赖 Python 的 Tcl/Tk。前端没有 npm 构建步骤。

## 测试与配置校验

```powershell
.\run.ps1 -SetupOnly
.\.venv\Scripts\python.exe main.py --config config.example.json --validate-config
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/frontend.test.cjs
node --check web/app.js
```

请用示例配置和临时目录测试，不要在自动化测试中修改真实持仓、运行数据或模型密钥。修改 Python 后台需重启进程，静态前端修改需刷新网页。

## 提交与发布

先初始化 Git 仓库并明确检查要提交的文件，再进行安全检查：

```powershell
git status --short
python tools/release.py check
```

检查工具以 Git 索引为准，拒绝被跟踪的运行数据、个人配置、虚拟环境、私钥文件、常见真实令牌和个人远程域名。它只报告文件位置与规则，不打印匹配到的秘密；这是辅助检查，不是完整的密钥审计。

从**已提交且工作区干净**的源码构建 ZIP：

```powershell
python tools/release.py build
```

输出位于忽略的 `dist/` 目录，文件名包含提交短哈希，另有 SHA-256 校验文件。构建读取指定提交中的文件而不是打包整个磁盘目录，因此本地 `data/` 和个人配置不会混入。ZIP 是 Python 源码分发包，不是 EXE 安装包。

不要提交或上传当前电脑的 Tailscale 访问策略、Agent 结果、数据库、日志、真实配置和任何密钥。不要直接压缩整个运行目录作为 GitHub 发布附件。
