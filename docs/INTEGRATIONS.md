# 平台接入与本地部署

本指南记录本项目已实现的接入方式。所有授权应在你自己部署的实例中完成；不要把密钥发到 Issue、公开截图或聊天中。

## 通用配置

默认地址 `http://localhost:8765`，默认时区 `Asia/Shanghai`。原生启动只监听 `127.0.0.1`。需要其他地址时，在启动前设置 `RELAY_ORIGIN`，并自行选择合适的 Uvicorn 监听地址。不要开启多个 worker。

原生启动示例：

```powershell
$env:RELAY_ORIGIN = 'http://localhost:8765'
$env:RELAY_TIMEZONE = 'Asia/Shanghai'
$env:RELAY_DATA_DIR = 'D:/relay-private-data'
python run.py
```

Docker 使用 `.env` 和 `compose.yaml`。环境文件不会进入镜像，凭据不进入前端构建产物。

## Gmail：OAuth 2.0

1. 在你自己的 Google Cloud 项目中启用 Gmail API。
2. 配置 OAuth 同意界面；个人开发时使用测试发布状态，添加自己的邮箱为测试用户。
3. 创建 Web application 类型的 OAuth 客户端。
4. 添加精确匹配的回调地址 `http://localhost:8765/oauth/google/callback`。远程部署应使用实际 HTTPS 地址。
5. 在“偏好设置 → 服务配置”填写客户端 ID 和密钥，立即生效；也可使用 `GOOGLE_CLIENT_ID` 和 `GOOGLE_CLIENT_SECRET` 环境变量后重启服务。
6. 解锁 Relay，在“连接中心 → Gmail”中发起授权，检查 Google 授权页面上的只读权限。
7. 返回工作区后点击“同步消息”，或等待下一轮自动同步。

```powershell
$env:GOOGLE_CLIENT_ID = '<你自己的客户端 ID>'
$env:GOOGLE_CLIENT_SECRET = '<你自己的客户端密钥>'
python run.py
```

使用 `https://www.googleapis.com/auth/gmail.readonly`。它属于受限范围；面向其他用户发布应用，可能需要 Google OAuth 验证及适用的安全评估。代码开源本身不等于每位部署者可以直接使用开发者的 OAuth 客户端。各用户应配置自己的应用，或由维护者建立经过相应审核的公共 OAuth 应用。

安全流程：短期 state 绑定当前管理会话；PKCE S256；固定回调地址；服务端换取和加密保存 refresh token；access token 过期前刷新。授权取消也会消费 state。Google 回调路径不记访问日志，避免授权码写入日志。

同步策略：首次查询最近 7 天收件箱，每页 100 条、每轮最多 10 页，分页检查点在成功写入消息后保存；每种来源按账号和消息 ID 去重。后续带 60 秒重叠窗口继续同步。此版本使用轮询，不实现 Gmail History 或删除事件同步；早于导入窗口的历史邮件不会自动导入。若 Google 使分页令牌过期，可断开后重新授权并从最近 7 天重建同步窗口。

官方资料：[OAuth Web Server](https://developers.google.com/identity/protocols/oauth2/web-server)、[Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)。

桌面版会在系统浏览器中完成 Google 授权，请在浏览器中使用相同的本地工作区密码登录。内嵌窗口和系统浏览器的会话 Cookie 分开，不会把登录令牌放进跳转链接；授权完成后桌面窗口会刷新连接状态。

## QQ 邮箱：专用授权码与 IMAP TLS

1. 登录 QQ 邮箱网页，在邮箱设置中查找第三方客户端 / POP3 / IMAP / SMTP 相关设置。
2. 开启 IMAP，按页面要求完成验证并生成专用授权码。平台设置名称可能调整，以当前页面为准。
3. 在 Relay 的 QQ 邮箱连接表单中填写 `@qq.com` 或 `@foxmail.com` 邮箱地址与授权码。
4. 点击“验证并连接”。程序验证成功后才加密保存授权码并导入邮件。

固定服务器 `imap.qq.com:993`，验证 TLS 证书。这里的授权码是 QQ 邮箱提供给第三方客户端的专用凭据，不是 OAuth，也不是 QQ 登录密码。

首次导入最近 50 封；后续使用 UID 与 UIDVALIDITY 游标，每轮顺序处理最多 50 封。使用只读选箱和 `BODY.PEEK[]`，不会在原邮箱标记已读。出于资源限制，超过 2 MB 的单封邮件跳过，附件不进入消息中心，建议前往原邮箱查看；当前界面不逐封列出跳过记录。

一个实例支持一个 QQ 邮箱连接。换邮箱会重新建立游标，原有消息保留。要撤销访问，在 QQ 邮箱中撤销专用授权码 / 关闭相关第三方服务。

官方入口：[QQ 邮箱帮助](https://service.mail.qq.com/)、[腾讯云邮件连接器说明](https://main.qcloudimg.com/raw/document/product/pdf/1270_46586_cn.pdf)。

## 飞书：企业应用事件接收

1. 在飞书开放平台创建企业自建应用。
2. 申请应用所需的消息事件权限，配置可用范围；需要管理员审批的步骤由管理员完成。
3. 配置 Encrypt Key 和 Verification Token，并在 Relay 中填写相同值。
4. 将事件请求地址设为 `https://你的域名/webhooks/feishu`，验证地址后订阅 `im.message.receive_v1`。
5. 发布 / 启用应用，将机器人加入需要的会话。机器人能够收到哪些消息取决于具体权限、会话类型和飞书规则。

**飞书云服务无法访问你电脑的 localhost。** 本地部署如需 HTTP 回调，必须提供可达的 HTTPS 入口，例如自己的反向代理；建议只对外转发 `/webhooks/feishu`，不要公开整个本地管理界面。飞书长连接是可考虑的后续方案，本版未实现。

实现要求：

- 原始请求体与 `timestamp + nonce + Encrypt Key` 一起计算 SHA-256 签名，使用常量时间比较。
- 请求时间偏差不超过 300 秒，拒绝未加密事件。
- 使用 SHA-256 派生 AES-256-CBC 密钥，解析加密包中的 IV，校验 PKCS7 填充与 Verification Token。
- 按事件 ID 记录 24 小时去重窗口；消息 ID 在数据库中继续持久去重。
- 目前只归集文本消息；图片、文件、音视频和交互卡片不导入。

保存配置显示“待验证”，收到有效事件后显示“已连接”。这不会赋予额外的聊天读取权限，也不会访问应用权限范围外的历史聊天。

官方实现参考：[飞书 Node SDK 事件校验](https://github.com/larksuite/node-sdk/blob/main/dispatcher/request-handle.ts)、[飞书 SDK AES 实现](https://github.com/larksuite/node-sdk/blob/main/utils/aes-cipher.ts)。

## 飞书：提醒发送

接收消息的企业应用与发送提醒的群机器人是两套独立配置。

1. 在准备接收个人提醒的飞书群中添加自定义机器人。
2. 开启签名安全校验，取得 Webhook URL 和签名密钥。
3. 在 Relay“连接中心 → 飞书提醒推送”填写配置。
4. 新的高优先级消息与日程提醒会进入投递流程；服务需要保持运行。

仅允许 `https://open.feishu.cn/open-apis/bot/v2/hook/…`，不会跟随重定向。签名按照飞书自定义机器人规则生成。推送内容包含提醒标题，避免将个人内容发送到不合适的工作群。

失败后指数退避，最多 5 次；超时可能造成重复投递，不提供严格 exactly-once 保证。达到上限的数量会显示在“偏好设置 → 提醒与通知”；本版没有手动重试队列界面。提醒中心仍可查看未确认的提醒。

官方资料：[自定义机器人指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。

## 微信与 QQ：当前的能力边界

当前版本没有实现企业微信或 QQ 机器人适配器，只有界面说明与统一消息结构，不能把演示消息当作真实接入。

个人微信与个人 QQ 的全量聊天读取不属于本项目提供的通用 OAuth 授权能力。不使用模拟登录、会话 Cookie 托管或未经官方支持的客户端协议。

后续扩展应分别评估企业微信应用消息、相关官方事件与 QQ 机器人事件。接收范围和主动发送限制需要查阅对应平台的当前规则，而不能只复用邮箱授权逻辑。

官方入口：[企业微信开发者中心](https://developer.work.weixin.qq.com/)、[QQ 机器人文档](https://github.com/tencent-connect/bot-docs)。

## HTTPS 私有部署

如需从其他设备访问，建议由 HTTPS 反向代理终结 TLS，后端仅监听本地 / 私有网络。使用环境变量：

```text
RELAY_ORIGIN=https://relay.your-domain.example
RELAY_SECURE_COOKIE=1
RELAY_ADMIN_PASSWORD=<至少12位的初始密码>
```

必须使用与浏览器访问一致的 Origin。远程部署不能依赖网页首次初始化，需预先设置初始密码。本应用不读取任意 `X-Forwarded-Host` 来决定 OAuth 回调或来源权限。

如果管理界面仍只在本机访问、仅代理飞书回调，保持 `RELAY_ORIGIN=http://localhost:8765`；飞书回调 URL 在平台中填写实际的公共 HTTPS 地址，界面显示的本机建议地址可忽略。

Windows 中保护数据目录时，请检查 NTFS ACL；`chmod(0600)` 的权限语义在 Windows 与 Unix 不完全相同。密钥与数据库同机保存只提供内容静态加密，不等于硬件密钥保护。


## Canvas / 学校作业平台

1. 确认学校允许个人 API 访问，按学校要求创建 Access Token。不能创建时先用通知邮件、手动事项与 CSV 课表。
2. 在“连接中心 → Canvas”填写学校 HTTPS 根地址（例如 `https://your-school.instructure.com`），不要填写登录页面路径或学校密码。
3. 填写授权令牌并点击“验证并保存”。验证读取当前用户资料和在读课程，凭据加密保存在本机且不回显。
4. 可按课程 ID 限定范围，并选择是否同步未来 14 天课程日历。点击同步，作业进入“待确认”。检查个人截止与条件后再启用提醒和排程。
5. 同步发现截止变化时保留原始个人记录与重要程度，显示来源最新日期，暂停截止提醒直至重新确认。发现已提交状态会提示核对，不代替用户标记本地完成。

此适配器只调用 GET，不提交作业、上传文件或修改成绩；个人令牌自身可能有更广权限。分页不会向其他站点转发令牌，也不跟随重定向。单次最多 25 门课程、每接口最多 3 页；达到上限会提示缩小范围。忽略未发布作业及超过 30 天的旧截止，首次不导入已提交作业。日历忽略全天事件，只将有起止时间的课程事件作为占用。学校其他教学系统需要独立适配器。

在“我的控制台”可以暂停同步，或继续接收邮箱消息但关闭自动拾取。暂停不会删除历史事项和提醒；如需取消提醒须修改事项或课程设置。断开连接仅删除本地令牌，请同时在 Canvas 侧撤销。

CSV 课表使用 UTF-8，列为 `title,start,end,location`，每次最多 200 行，时间必须包含时区。示例：

```csv
title,start,end,location
概率论,2026-11-02T09:00:00+08:00,2026-11-02T10:30:00+08:00,A203
```

参考：[Canvas 授权](https://developerdocs.instructure.com/services/canvas/oauth2/file.oauth)、[作业接口](https://developerdocs.instructure.com/services/canvas/resources/assignments)、[课程日历接口](https://developerdocs.instructure.com/services/canvas/resources/calendar_events)。
