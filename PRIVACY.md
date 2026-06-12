# Privacy Policy

Last updated: 2026-06-12

This policy applies to the Discord Q&A bot in this repository, also referred to as "the bot" below. The bot is intended for Discord server Q&A, moderation support, message summarization, thread review, and related server administration workflows.

## Data We Process

The bot may process the following Discord data when needed for enabled features:

- Discord user IDs, usernames, display names, server IDs, channel IDs, thread IDs, message IDs, message links, role IDs, and command usage metadata.
- Message content, attachments, embeds, and attachment metadata for AI Q&A, mention replies, image checks, message summaries, thread review, context export, quick punishment evidence forwarding, and bot-to-bot punishment sync.
- Member and role data for permission checks, trusted-user synchronization, quick punishment, punishment reversal, and role-based access control.
- Uploaded knowledge-base files and bot configuration created by server administrators or authorized users.

The bot does not request or use Discord Presence data such as online status, custom status, activity, or game presence.

## Why We Process Data

The bot processes data only to provide its server features:

- Generate AI-assisted answers when a user mentions the bot or an authorized user invokes a Q&A context command.
- Summarize or evaluate message history when an authorized command requests it.
- Review forum threads for unresolved questions and apply configured labels.
- Enforce role-based access control, cooldowns, daily limits, blacklists, and moderation records.
- Provide administrative utilities such as message sending, log export, context export, and broadcast configuration.

## Storage

The bot stores some data outside Discord on the server where it is deployed:

- Local SQLite databases for bot admins/trusted users, quick punishment records, tag records, and thread review cache.
- Local JSON files for bot settings, allowed threads, blacklists, usage counters, broadcast configuration, and role sync configuration.
- Runtime logs and limited prompt/debug archives. Q&A prompt archives are kept in a small rolling set by default.
- Temporary image or context files while processing requests. Context exports are intended for short-lived delivery to authorized users.

Stored moderation records may include user IDs, usernames, executor IDs, reasons, timestamps, message links, channel IDs, and removed role IDs. The bot does not intentionally store full message content in moderation databases, but message content may appear in prompt/debug archives, temporary exports, runtime logs, or Discord log channels when an enabled feature requires it.

## AI and Third-Party Processing

For AI features, the bot sends the relevant prompt, selected message content, selected context, and selected image data to the OpenAI-compatible API endpoint configured by the bot operator. This is done only to generate a response, summary, classification, or review result for the requested bot feature.

The bot operator is responsible for choosing an API provider and configuring that provider according to the provider's data-processing terms.

## Machine Learning Training

The bot does not use Discord message content, attachments, member data, or presence data to train machine learning or AI models.

## User Controls

Users can avoid AI processing by not invoking the bot, not mentioning the bot in enabled threads, and not submitting messages or attachments to bot commands. Thread owners or authorized moderators can disable the bot in a thread, manage thread blacklists, and remove users from bot access lists where those features are enabled.

Users may contact the server administrators or bot operator to request removal from bot-specific blacklists or access lists, request deletion of bot-side records where operationally possible, or ask that a thread be disabled for bot responses. Deleting a Discord message may prevent future processing of that message, but it may not remove already-created bot logs, moderation records, or Discord messages already sent by the bot.

## Access and Security

Access to stored bot data is limited to the bot operator and authorized server administrators with access to the deployment environment or administrative bot commands. Bot commands that expose logs, configuration, moderation records, or message exports are restricted to configured administrators or trusted users.

The bot does not sell user data.

## Data Retention

Retention depends on feature configuration and server operation:

- Temporary context export files are deleted after a short delivery window by the bot feature that creates them.
- Prompt/debug archives and runtime logs are limited or should be periodically cleaned by the bot operator.
- Moderation, permission, role sync, blacklist, usage, and thread review records are retained until deleted by the bot operator or overwritten by normal bot operation.
- Uploaded knowledge-base files and configuration files remain until removed by an authorized user or bot operator.

## Contact

For privacy requests, contact the server administrators or the bot operator responsible for the Discord server where the bot is installed.

---

# 隐私政策

最后更新：2026-06-12

本政策适用于本仓库中的 Discord 答疑机器人。机器人用于 Discord 服务器内的答疑、AI 总结、帖子状态检查、管理辅助和权限控制。

机器人会在功能需要时处理 Discord 用户 ID、用户名、显示名、服务器/频道/子区/消息 ID、消息链接、身份组 ID、消息内容、附件信息、成员身份组信息和管理员配置。机器人不会请求或使用在线状态、活动状态、游戏状态等 Presence 数据。

AI 功能会把相关消息内容、上下文和选中的图片数据发送到机器人运行者配置的 OpenAI 兼容 API，用于生成答复、总结或分类结果。机器人不会用 Discord 消息内容或用户数据训练机器学习或 AI 模型。

机器人会在部署服务器本地保存部分数据，包括 SQLite 数据库、JSON 配置、运行日志、有限的 prompt/debug 归档、临时图片或上下文导出文件。管理记录可能包含用户 ID、用户名、执行人、原因、时间戳、消息链接、频道 ID 和被移除身份组 ID。管理数据库通常不保存完整消息正文，但消息正文可能出现在 prompt/debug 归档、临时导出、运行日志或 Discord 日志频道中。

用户可以通过不提及机器人、不使用相关命令、不向机器人提交附件或消息来避免主动触发 AI 处理。子区楼主或授权管理员可以在支持的功能中关闭子区答疑、管理黑名单或调整访问权限。用户也可以联系服务器管理员或机器人运行者，请求移除访问/黑名单记录、删除可删除的机器人侧记录，或关闭某个子区的机器人答疑。

机器人不会出售用户数据。隐私请求请联系安装该机器人的 Discord 服务器管理员或机器人运行者。
