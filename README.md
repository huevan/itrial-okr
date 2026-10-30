# iTrial OKR

基于 **FastAPI + SQLite + 原生 HTML/CSS/JavaScript** 的企业 OKR 管理系统。可独立运行，也可通过 `okr.public` 挂载到现有 FastAPI 项目。前端无需 Node.js 构建。

## 功能

- 多公司、部门和成员管理，服务端角色权限与公司数据隔离。
- 公司年度/月度目标、部门与个人目标、KR、权重和进度计算。
- 目标提交、审批、撤回、状态流转、脑图和统计看板。
- 企业微信 OAuth 登录、组织同步及首次短信验证手机号。
- 逐 O 自评与主管评估、成员月度总评、Excel/打印及中文 PDF 评估表。
- 审计日志和超级管理员应急密码登录。

逐 O 得分为自评 20% + 主管评分 80%，KR 进度仅展示。月度总分为 `(任务分 × 70% + 行为分 × 30%) × 红线系数`。

## 核心功能截图

以下为维护者提供的既有部署界面截图，部分信息已打码，原图保留。截图用于展示功能布局，画面中的组织、成员和统计属于截图所示环境，并非开源项目内置的演示数据。

### 目标对齐与脑图

以脑图查看目标层级、成员关联、对齐关系及完成进度。

![目标对齐与脑图](docs/screenshots/okr-alignment-map.png)

### 个人 OKR 与 KR 编辑

填写目标与描述、对齐公司月度目标，设置周期、目标权重及多项 KR 的目标值、当前值和权重。

![个人 OKR 与 KR 编辑](docs/screenshots/okr-objective-editor.png)

### 统计报表

按周期查看部门平均完成率、跨部门对比、成员完成情况与提交情况。

![统计报表](docs/screenshots/okr-statistics.png)

### 成员月度评估

查看任务目标、行为目标、红线系数和月度总分，并导出成员 PDF 评估表。

**历史界面说明：此图内单 O 评分文字属于旧版本。当前代码采用“自评 20% + 主管评分 80%”，KR 完成率不参与计分；任务 70%、行为 30% 和红线系数的规则保持。**

![成员月度评估](docs/screenshots/okr-monthly-evaluation.png)

### 后台用户管理

按公司和角色筛选成员，管理用户、所属部门、账号状态及权限。

![后台用户管理](docs/screenshots/okr-admin-users.png)

## 快速开始

需要 Python 3.12 或 3.13。推荐使用独立虚拟环境。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts/setup.py
uvicorn app:app --host 127.0.0.1 --port 8888
```

Linux/macOS 使用 `source .venv/bin/activate` 激活环境，其余命令相同。

初始化会交互询问管理员手机号和密码，生成随机 JWT 密钥和本地数据库，不会显示密码，不会覆盖已有配置或数据。自动化环境可通过 `OKR_BOOTSTRAP_PHONE`、`OKR_BOOTSTRAP_PASSWORD` 提供初始化值。仓库不提供通用默认密码。

- OKR 页面：<http://127.0.0.1:8888/okr>
- 登录：<http://127.0.0.1:8888/okr/login>
- 管理页：<http://127.0.0.1:8888/okr/admin>
- API 文档：<http://127.0.0.1:8888/docs>
- 健康检查：<http://127.0.0.1:8888/healthz>

普通成员通过企业微信登录；首次使用需配置企业微信和腾讯云短信。未配置时页面可打开，但成员登录会明确提示配置缺失。超级管理员应急入口为 `POST /api/okr/auth/login`，请求字段为 `phone`、`password`；返回令牌用于 Bearer 鉴权。该接口不用于普通成员密码登录。

## 配置

`scripts/setup.py` 根据 [config.example.json](config.example.json) 创建本地 `config.json`。真实配置、数据库、日志和 `.env` 均被 Git 忽略，不能上传。

企业微信配置位于 `okr.wecom`，回调路径为 `/api/okr/auth/wecom/callback`。配置企业自己的可信域名、CorpID、AgentID 和 Secret；`enabled` 设为 `true` 后生效。支持以下环境变量覆盖：

```text
OKR_WECOM_ENABLED
OKR_WECOM_CORP_ID
OKR_WECOM_AGENT_ID
OKR_WECOM_APP_SECRET
OKR_WECOM_CALLBACK_URL
OKR_WECOM_CORP_NAME
OKR_WECOM_COMPANY_ID
OKR_WECOM_LEADER_ROLE
```

短信配置位于 `okr.sms`，支持 `TENCENTCLOUD_SECRET_ID`、`TENCENTCLOUD_SECRET_KEY`、`TENCENT_SMS_SDK_APP_ID`、`TENCENT_SMS_SIGN_NAME`、`TENCENT_SMS_TEMPLATE_ID`。密钥只放在本地配置或部署环境的秘密存储中。实际短信调用使用运营方自己的供应商账号。

## Docker

先按上面的步骤完成本地初始化，再运行：

```sh
docker compose up --build -d
```

如果已有服务占用 8888，请修改 `compose.yaml` 的主机端口。容器仅挂载本地配置和 `data/`，数据库随主机目录持久化。Linux 绑定挂载时，`data/` 需要允许容器用户 UID 10001 读写，`config.json` 也需对该用户可读；不要给其他用户开放读写。

## 目录与集成

```text
app.py                    # 独立应用：生命周期、路由、静态资源
okr/
  public.py               # 对外集成入口
  api.py                  # 现有 OKR API 与页面路由
  auth.py / models.py     # 鉴权、请求校验
  db.py / paths.py        # SQLite、稳定资源路径
  evaluation*.py          # 月度评估、评分、PDF
  progress.py             # 进度规则
  wecom.py / sms.py       # 外部供应商适配器
static/okr/               # 页面、JS、CSS、通用标识
tests/                    # 隔离 SQLite 与供应商 mock 回归
scripts/                  # 初始化与质量检查
okr_*.py                  # 历史导入兼容别名，不包含第二套实现
```

集成项目可使用 `from okr.public import okr_router, init_db`，在生命周期中调用 `init_db()`，通过 `app.include_router(okr_router)` 注册路由，并挂载 `/static/okr`。配置、静态文件和数据库路径相对于项目根目录解析，不受启动工作目录影响。

## 验证

```sh
python -m pip check
python scripts/verify.py
```

验证脚本检查架构、发布安全和 Python 语法，然后使用临时配置及临时 SQLite 数据库运行全部 OKR 测试，不调用真实企业微信或短信服务。它不会创建或覆盖本地 `config.json`。严格静态类型检查尚未建立，已有大型路由的重构边界记录在 `.engineering/`，不把语法检查视为完整类型验证。

原平台的 `/okr`、`/api/okr/*` 路径和业务契约保持；独立应用额外提供 `/` 跳转和 `/healthz`。公开副本只调整首次初始化的固定密码及演示标识，不携带原平台运行配置、历史数据库、内部部署脚本或 Git 历史。功能截图由维护者单独提供，不包含可供导入的业务数据。

## 开源许可

本项目采用 [GNU AGPL-3.0](LICENSE)。PDF 导出使用 PyMuPDF，其官方提供 AGPL 和商业许可两条路线，参见 [PyMuPDF licensing](https://pymupdf.io/licensing)。保留 PyMuPDF 生成的 PDF 元数据。浏览器依赖与各自许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

发布和运维记录见 [docs/status.md](docs/status.md)。贡献代码前请运行验证脚本，并保持现有接口、权限、租户隔离与评分规则。
