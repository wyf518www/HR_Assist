# HR Assist · HR 自动化工作台

面向单用户的自托管人事工作台，第一版提供招聘流程助手。首页先选择业务模块；员工档案、入转调离、考勤等仍为待规划。

部署目标：群晖 DS423+（x86_64）、Docker / Container Manager，通过群晖反向代理和 HTTPS 访问。前端、API、任务调度在同一容器运行，SQLite 和附件挂载到 NAS。无需互联网 SMB。

## 第一版功能

- 单用户登录、会话与 CSRF 校验；邮箱和模型密钥加密存储。
- 网页上传 PDF 简历和 PDF / DOCX / TXT 岗位 JD，按 `【岗位_姓名_其他】` 分类；保留原件、哈希、来源和历史 JD。
- QQ IMAP TLS 只读收件箱、主题过滤、增量窗口、去重；北京时间每日 09:00 计划默认关闭。
- PDF 文本解析、中英文 OCR；扫描文字可对照原件核验，核验后保留版本并重新评估。
- 公司批准的 JSON 对话模型接口做逐项证据判定，程序核验引文并计算 20/30/30/10/10 分值。缺证据显示暂计分、待定，JD 不完整则不评估。
- 下载“人工跟进.xlsx”，Excel 编辑五个人工字段后回传；按导出基准逐字段合并，冲突整批拒绝。生成“招聘推荐总览.xlsx”（15列 + 证据表）与岗位 HTML 报告。
- 人工反馈统计、可修改并版本化的评分细则、显式重评；不自动训练或调整规则。
- 工作台内通知、任务记录、JSONL 审计日志、异常隔离和任务续跑。

## 部署与使用

1. 阅读 [群晖部署指南](docs/群晖部署指南.md)，准备数据目录、管理员密码文件、HTTPS 域名与 `.env`。
2. 在 Container Manager 中创建项目，使用仓库根目录的 `docker-compose.yml` 构建并启动。
3. 登录工作台，创建并确认岗位 JD；配置 QQ IMAP 授权码、公司批准的模型接口，以及已核验的院校名单。
4. 先用合成或获准使用的样本核对报告与 Excel 回传结果，再启用每天的计划。

源码仓库：[wyf518www/HR_Assist](https://github.com/wyf518www/HR_Assist)。不包含生产凭据或真实候选人资料。

## 本地开发

Python 3.10+；生产镜像使用 Python 3.12。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:HR_ADMIN_PASSWORD = '请替换为至少12位的本机测试密码'
$env:HR_PUBLIC_URL = 'http://127.0.0.1:8766'
$env:HR_SECURE_COOKIE = 'false'
$env:HR_SCHEDULER = 'false'
.\.venv\Scripts\python.exe -m uvicorn hr_assist.main:create_app --factory --host 127.0.0.1 --port 8766 --workers 1
```

本地 OCR 需要另行安装带 `chi_sim` 和 `eng` 的 Tesseract；生产镜像已包含。`runtime/` 为默认本地数据目录，已被 Git 忽略。不要将开发模式暴露到公网。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check hr_assist/web/app.js
```

CI 在 Linux 上运行测试、构建镜像，并验证容器登录、扫描 PDF OCR 和重启后数据保留。实际检查结果见 [第一版实施与验收](docs/第一版实施与验收.md)。

## 目录

| 路径 | 用途 |
| --- | --- |
| `hr_assist/` | FastAPI、SQLite、工作流、模型与报表服务 |
| `hr_assist/web/` | 真实服务的中文前端 |
| `tests/` | 合成数据业务测试 |
| `docker-compose.yml`、`Dockerfile` | 群晖容器部署 |
| `prototype/` | 历史可点击原型，与真实服务独立 |
| `config/recruitment.example.json` | 历史 Windows 示例，当前服务不读取 |

第一版为单实例、单用户；没有多用户权限、自动发送候选人资料、外部通知推送或自动录用能力。模型仍可能误判，逐字引文校验不能代替人工核验事实与结论。
