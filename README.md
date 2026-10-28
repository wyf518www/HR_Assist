# HR Assist · HR 自动化工作台

面向单用户的自托管人事工作台，第一版提供招聘流程助手。首页先选择业务模块；员工档案、入转调离、考勤等仍为待规划。

部署目标：支持 Docker / Container Manager 的 x86_64 群晖，通过群晖反向代理和 HTTPS 访问。前端、API、任务调度在同一容器运行，SQLite 和附件挂载到 NAS。无需互联网 SMB。

## 第一版功能

- 单用户登录、会话与 CSRF 校验；邮箱和模型密钥加密存储。
- 网页上传 PDF 简历和 PDF / DOCX / TXT 岗位 JD；大模型结合附件名、邮件主题和简历文字识别姓名与应聘岗位，不按固定文件名格式拆分。保留原件、哈希、来源和历史 JD。
- “姓名与岗位核对”支持批量分析旧记录、展示原文依据并确认修正；保留记录 ID、人工跟进和历史评分。不明确的身份或岗位等待人工核验。
- JD 支持批量上传和大模型结构化提取，后台逐份处理、原文证据校验、缺失项提示、失败重试；人工核对后创建岗位或保存新的 JD 版本。操作说明见 [JD 批量导入](docs/JD批量导入.md)。
- QQ IMAP TLS 只读收件箱、主题过滤、增量窗口、去重；北京时间每日 09:00 计划默认关闭。
- PDF 文本解析、中英文 OCR；扫描文字可对照原件核验，核验后保留版本并重新评估。
- 公司批准的 JSON 对话模型接口做逐项证据判定，程序核验引文并计算 20/30/30/10/10 分值。缺证据显示暂计分、待定，JD 不完整则不评估。
- 网页与“人工跟进.xlsx”共用七个人工跟进字段；Excel 按导出基准逐字段合并，冲突整批拒绝。生成“招聘推荐总览.xlsx”（17列 + 证据表）与岗位 HTML 报告。
- 候选人库支持多选后批量跟进：只改勾选字段，先预览人员和覆盖内容再确认；支持明确清空，任一记录版本冲突则整批不写入。
- 候选人库支持组合筛选姓名/岗位/附件名、岗位、处理状态、推荐等级、匹配度区间和面试跟进；可按匹配度、处理状态、推荐日期、入库时间或约面时间双向排序。未评估不等于0分，空值始终置底。每页50/100/200/500人，批量跟进只选择本页，筛选、排序和翻页会清空选择。
- 人工反馈统计、可修改并版本化的评分细则、显式重评；不自动训练或调整规则。
- 工作台内通知、任务记录、JSONL 审计日志、异常隔离和任务续跑。
- 详细诊断记录处理阶段、耗时、错误码及技术元数据，不记录凭据或简历正文；默认保留7天，每天北京时间03:00清理，业务记录和审计日志保留。
- 页面按需加载，候选人列表分页，切换页面时防止较早的请求覆盖当前内容。
- 智谱官方标准接口的 GLM-5.3 支持配置思考强度与最大输出 token；预设填入 `low / 32768`，保存后生效。该模型不能关闭思考；程序仍拒绝空正文、截断结果和无法核验的引文。
- 提供 Windows 经 SSH 更新群晖的脚本，支持累计离线更新包、完整备份与健康检查。使用说明见 [Windows 更新工具](scripts/deploy/使用说明.txt)。

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
node --test tests/test_page_navigation.js tests/test_model_settings.js tests/test_candidate_query.js tests/test_candidate_list.js
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

姓名识别会向已批准的模型发送含姓名的资料，先过滤常见联系方式和证件号码；评分步骤再移除已识别姓名。两个步骤共用每批次调用上限，完成的结果可复用。旧记录没有保留邮件主题时，只使用现存附件名与简历文字。批量核对只生成建议，确认更正姓名或岗位后保留历史评分，需重新处理待办才能生成新评分。
