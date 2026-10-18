# 官方能力与已有工作调研

整理日期：2026-10-18。第三方平台接口与审核规则会变化，应以部署时的官方资料为准。

## 为什么选择独立的小型框架

项目需要统一的中文工作台、个人收件箱和日程模型，同时保持本地部署简单。第一版使用 FastAPI、SQLite 与原生前端实现核心流程，避免把大型工作流平台作为必需依赖。以下项目用于架构研究，不捆绑其源码或服务。

| 已有工作 | 借鉴方向 | 当前关系 / 许可证 |
| --- | --- | --- |
| [Huginn](https://github.com/huginn/huginn) | 事件生产 / 消费、邮箱与 Webhook、个人自动化 | 非依赖；MIT |
| [Apprise](https://github.com/caronc/apprise) | 统一多渠道通知适配器 | 非依赖；BSD-2-Clause；适合作为后续通知层候选 |
| [Novu](https://github.com/novuhq/novu) | 通知收件箱、工作流与投递体验 | 非依赖；open core，核心 MIT，企业部分另有商业许可 |

引入其他项目代码时应重新核对具体文件与版本的许可证，不能只因仓库公开就假设可无条件复用。本项目独立实现的代码使用 MIT。

## 官方资料与实施决定

| 官方资料 | 本项目中的决定 |
| --- | --- |
| [Google OAuth Web Server](https://developers.google.com/identity/protocols/oauth2/web-server) | 服务端授权码流程，PKCE、单次 state、refresh token 加密保存 |
| [Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) | 仅申请 gmail.readonly；明确受限权限的审核条件 |
| [QQ 邮箱帮助](https://service.mail.qq.com/) / [腾讯云邮件连接器说明](https://main.qcloudimg.com/raw/document/product/pdf/1270_46586_cn.pdf) | 专用授权码接入 IMAP；不把邮箱授权码描述成 OAuth |
| [飞书官方 SDK 签名校验](https://github.com/larksuite/node-sdk/blob/main/dispatcher/request-handle.ts) | 对原始请求体验签，校验时间窗口和 Verification Token |
| [飞书官方 SDK 加密包格式](https://github.com/larksuite/node-sdk/blob/main/utils/aes-cipher.ts) | SHA-256 派生 AES 密钥，包内 IV，AES-CBC 解密 |
| [飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot) | 接收消息与发送群提醒分别配置；发送端开启签名 |
| [QQ 官方机器人文档](https://github.com/tencent-connect/bot-docs) | 只规划官方机器人场景，不承诺读取个人全部会话 |
| [企业微信开发者中心](https://developer.work.weixin.qq.com/) | 企业应用适配器单独规划，当前未实现 |

## 尚待实际联调

- 用户自建 Google Cloud OAuth 应用的授权、刷新令牌和网络可达性。
- 用户 QQ 邮箱 IMAP 开关、授权码与各类邮件编码。
- 飞书应用在企业内的权限审批、消息场景、加密回调及自定义机器人配置。
- Docker 镜像在目标设备上的运行与 HTTPS 反向代理配置。

测试使用合成的加密事件和模拟平台响应；不会拿公开演示内容冒充真实接入结果。


## 备忘与学生场景补充

[Super Productivity](https://github.com/super-productivity/super-productivity)（MIT）将外部任务整合与时间安排结合，可借鉴其将收集、确认和工作时段分开的交互方式。本项目的备忘评分、遗漏检查和课表避让独立实现，未复制该项目代码。Canvas 集成按官方 [Assignments](https://developerdocs.instructure.com/services/canvas/resources/assignments) 与 [Calendar Events](https://developerdocs.instructure.com/services/canvas/resources/calendar_events) 文档实现；课程日历每请求最多 10 个 context code，因此适配器分组读取。
