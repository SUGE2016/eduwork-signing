# EduWork 签名服务

[English](README_EN.md)

由仓库所有者控制的 EduWork macOS 签名、公证和分发流水线。上游无需获得 Apple 证书或公证凭据，也无需逐版本人工审批。

## 工作方式

每约 10 分钟检查 `ECNU/EduWork` 的新公开 Release。获取 macOS arm64 DMG（优先）或 ZIP，使用固定脚本签名，提交 Apple 公证。在独立、无签名凭据的 runner 上验证票据、Gatekeeper 与应用启动，成功后在本仓库发布签名 DMG 和 `delivery.json`。

GitHub 定时任务可能延迟，不保证精确每 10 分钟执行。默认只处理本仓库创建之后发布的版本，避免批量签署历史版本。可手动指定旧版本进行测试。

当前支持 `org.eduwork.eduwork.electron`、文件名 `EduWork-<version>-macos-arm64-electron.dmg/zip`。来源限定为 `ECNU/EduWork`，`SUGE2016/EduWork` 仅用于所有者测试。DMG 输入保留原 Finder 布局；ZIP 输入生成基础拖拽安装窗口。

当前上游仍需对接“Tag → 构建 → 发布候选包 → 接回签名包”的工作流；仅创建本仓库不会自动修改上游。此服务目前发现的是**已公开 Release 的安装包**，不是任意 Actions artifact。Windows 不经过这条 Apple 签名流程。

## 一次性配置

在 Settings → Environments 创建 `signing`，只允许 `main` 部署，无需 required reviewers。在其中配置：

| Environment Secret | 内容 |
| --- | --- |
| `MACOS_CERTIFICATE_P12_BASE64` | 含私钥的 Developer ID Application `.p12` 的 Base64 |
| `MACOS_CERTIFICATE_PASSWORD` | `.p12` 导出密码 |
| `APPLE_ID` | 开发者 Apple ID |
| `APPLE_APP_SPECIFIC_PASSWORD` | Apple App 专用密码 |

仓库 Variables：

| Variable | 内容 |
| --- | --- |
| `APPLE_TEAM_ID` | 与签名证书一致的团队 ID |
| `SOURCE_REPO` | 默认 `ECNU/EduWork` |
| `SIGNING_ENABLED` | 凭据配置并验证后设为 `true`，启用定时处理 |
| `SIGN_FROM` | 可选 ISO UTC 时间；默认本仓库创建时间 |

初次先从 Actions 手动运行，填写已公开的 `source_tag`，保留 `inspect_only=true` 检查来源。配置凭据后关闭 `inspect_only` 跑一次完整流程，再启用定时任务。

也可以在自己的终端运行 `python3 scripts/configure_secrets.py --p12 /完整路径/证书.p12`，按提示输入导出密码、Apple ID 和 App 专用密码。秘密通过标准输入传给 `gh secret set`，不会写入命令行参数或本地配置文件。

凭据不写入代码、日志、产物；`.cer` 不含私钥，不能替代 `.p12`。GitHub Secrets 加密存储不改变私钥已托管到 GitHub 的事实。上游没有此仓库写权限。不要把签名 job 配置到 `pull_request`、`pull_request_target` 或任意分支，也不要允许上游修改签名脚本。

## 状态、失败和重试

- 源 Release ID 对应本仓库 `signed-<ID>`，不会因定时查询重复创建。
- 签名后先保存为 draft，保存输入/输出哈希、commit、asset ID 和公证记录。draft 不公开。
- `In Progress` 保留草稿，下次查询同一提交，不重复签名。超时不等于通过。
- 上传完成但保存提交 ID 前意外终止时，通过包含输入 asset ID 与文件哈希的唯一提交名恢复。若 Apple 仍停留在未完成上传状态，需要所有者检查，不自动重复提交。
- 公证拒绝时草稿保存 `notary-log.json`，停止新签名，等待所有者处理。可关闭 `SIGNING_ENABLED` 暂停所有定时操作。
- 不完整草稿不会被自动删除或跳过；先检查日志，再由所有者清理或修复。
- 发布前再次核对源 Tag 对应的完整 commit、源 asset ID 及验收报告对应的最终哈希。
- 公证通过但上传票据后状态写入中断可能出现哈希不匹配，此时停止并要求人工核对，不猜测或发布。

`verification.json` 明确记录验证范围：有下载标记的 DMG/App Gatekeeper 评估；独立 runner 本地副本启动到 `dsh-app://app/` 页面。交互式首次打开确认、模型会话、Office 和音视频完整功能验收不在自动冒烟范围内。

源码来源绑定于 GitHub 发布记录、Tag commit、不可变 asset ID 和 SHA-256；目前不要求上游提供构建证明，也不声称它是可复现构建或源代码安全审计。

## 上游自动接回安装包

1. 上游 Tag 工作流先构建、测试，发布**预发布候选 Release**，附带原 macOS ZIP/DMG；其发布说明由上游维护。
2. 本服务自动完成签名、公证和验收，在此仓库公开 `signed-<源 Release ID>`。
3. 上游通过定时任务查询本仓库交付，在受信任的固定脚本版本中运行：

```sh
python3 scripts/fetch_signed.py --upstream ECNU/EduWork --release-id "$SOURCE_RELEASE_ID" --output signed
```

`fetch_signed.py` 和同目录 `service.py` 应从本仓库**固定 commit** 获取。它校验源仓库、Release ID、Tag、commit、asset ID、验收状态和最终文件哈希。尚未交付时应稍后再查，不在 macOS runner 上长时间等待。

4. 上游使用自己的 `GITHUB_TOKEN` 把 `signed/*-signed.dmg` 和交付记录上传到同一个 Release，全部平台准备好后再转正式发布。不得重新打包已签名 DMG，不得把这里的哈希当成原 ZIP 的哈希。

签名仓无上游写权限。若上游不安装接回步骤，用户仍可在本仓库 Releases 下载公证版。不要覆盖原 Sparkle 更新 ZIP、appcast 或其签名；Apple 代码签名与 Sparkle 更新签名不是同一套凭据。

## 本地检查

```sh
python3 -m unittest discover -s tests -v
```

所有签名脚本只处理安装包数据，不执行上游提供的安装钩子或脚本；真正启动应用的工作位于无 Apple 凭据的独立 job。此策略仍以信任上游发布内容为前提，并不把 Apple 公证当作完整安全审计。
