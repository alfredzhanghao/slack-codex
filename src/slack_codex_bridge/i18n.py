"""Translate bridge-owned UI only. Native/model payloads never enter this catalog."""

import re


class Literal(str):
    """A presentation string already localized or containing literal native text."""

ZH = {
    "Steer current task": "引导当前任务",
    "This chat is working. Use Codex desktop to guide the active task; Slack Steer cannot safely target this turn.": "当前会话正在执行任务。请在 Codex 桌面端追加引导，当前连接无法保证 Slack 引导送达原轮次。",
    "This connection does not provide account limits. View them in Codex desktop.": "当前连接不提供账户额度，请在 Codex 桌面端查看。",
    "Account limits are temporarily unavailable.": "账户额度暂时无法获取。",
    "Activity unavailable; projects are listed by path order. Refresh to retry.": "暂时无法获取活跃时间，项目按固定目录顺序排列，请刷新重试。",
    "Click Take control on the session card before sending attachments and instructions. The shared files remain in Slack; no task was started.": "请先在会话卡片点击“接管输入”，再发送附件和指令。已分享的文件仍保留在 Slack，没有启动任务。",
    "Session thread ready. After taking control, write your task here. No task started.": "会话线程已就绪。接管输入后，在这里发送任务。本次没有启动任务。",
    "Open the session card's replies to continue. Web fallback may open a browser.": "点击会话卡片的回复数进入线程。备用网页入口可能打开浏览器。",
    "Open thread (web fallback)": "打开线程（备用网页入口）",
    "Live status unavailable; open this chat in Codex desktop, then Refresh.": "历史记录可查看；实时状态暂不可用，请在 Codex 桌面端打开此会话后刷新。",
    "Steer in Slack is unavailable: this connection cannot guarantee the original active turn. Use Codex desktop.": "当前连接无法保证引导送达原执行轮次，暂不支持在 Slack 引导，请使用 Codex 桌面端。",
    "Start": "启动", "Stop": "停止", "Exit": "退出", "Restore connection": "恢复连接",
    "Refresh instances": "刷新实例", "Connect selected": "连接所选实例", "Settings": "设置",
    "Slack Codex Bridge Supervisor": "Slack Codex 桥接管理程序",
    "Slack Codex Bridge — on-demand supervisor": "Slack Codex 桥接 — 按需守护程序",
    "Running Codex instances — select and explicitly confirm": "运行中的 Codex 实例 — 请选择并确认",
    "Scanning running instances…": "正在查找运行中的实例…",
    "Close hides this window. Use Exit to stop the managed bridge and monitoring.": "关闭窗口会隐藏到托盘。点击退出才会停止受管桥接和监控。",
    "Select an instance, then Connect selected. Refresh after two minutes.": "选择实例后点击连接。列表超过两分钟请刷新。",
    "Refresh instances to choose a connection, or use Start if no desktop is open.": "刷新实例后选择连接；未打开桌面时可点击启动。",
    "Choose and confirm an existing compatible instance to connect without restarting it.": "选择并确认现有兼容实例即可连接，无需重启。",
    "Private or unsupported transports remain running; detection does not grant access.": "私有或不受支持的实例保持运行；检测到实例不代表已获得连接权限。",
    "Stop / Exit affects only this supervisor's bridge. Native tasks continue.": "停止或退出只影响此程序管理的桥接，Codex 原生任务继续运行。",
    "No login registration. No task, approval or uncertain upload is replayed.": "不会注册开机启动，不会重放任务、审批或结果不确定的上传。",
    "Select a running instance first.": "请先选择运行中的实例。", "Choose an instance": "选择实例",
    "Cannot attach this instance": "无法连接此实例", "Confirm existing Codex instance": "确认现有 Codex 实例",
    "No process will be restarted or terminated.": "不会重启或终止任何进程。",
    "Verified desktop version:": "已核验桌面版本：",
    "Connect the bridge to this existing instance? Native processes stay running.": "是否将桥接连接到此现有实例？原生进程保持运行。",
    "IPC mode supports loaded chats, tasks, exact-turn Stop and files.": "IPC 模式支持已加载的会话、任务、精确停止当前轮次和文件传输。",
    "Use the desktop for Steer, approvals, model/Skills settings, Compact and Fork.": "追加指令、审批、模型/技能设置、压缩和分叉请在桌面操作。",
    "No verified shared endpoint; this desktop will stay running": "没有已核验的共享接口，此桌面保持运行",
    "Executable differs from the configured desktop": "程序路径与配置的桌面不同",
    "Version is incompatible with this bridge profile": "版本与当前桥接配置不兼容",
    "Binary changed; verify the deployment before connecting": "程序文件已变化，请核验部署后再连接",
    "An earlier native start is unconfirmed; inspect it first": "上次原生启动结果未确认，请先检查",
    "Instance belongs to a different Windows user or login session": "实例属于其他 Windows 用户或登录会话",
    "Identity or connection unavailable; refresh or verify the profile": "身份或连接不可用，请刷新或核验配置",
    "Bridge settings": "桥接设置", "Language": "语言", "Automatic document/image delivery": "自动发送文档和图片",
    "Automatic threshold (MB; below only)": "自动发送阈值（MB，仅小于此值）",
    "Maximum output file size (MB; 1–100)": "输出文件上限（MB，1–100）",
    "Enabled": "启用", "Disabled": "关闭", "Save settings": "保存设置",
    "Saved. Applies to subsequent Windows/Slack UI and file decisions; no Codex restart.": "已保存，Windows 和 Slack 的后续界面与文件发送使用新设置。无需重启 Codex。",
    "Enter whole MB values from 1 to 100.": "请输入 1 到 100 的整数 MB。",
    "Videos always require confirmation. Files at the threshold also require confirmation.": "视频始终需要确认，达到阈值的文件也需要确认。",
    "Take control": "接管输入", "Return to desktop": "交回桌面", "Refresh": "刷新",
    "Enter chat thread": "进入会话线程", "Open chat": "打开会话", "Choose project": "选择项目",
    "View chats": "查看会话", "More projects": "更多项目", "Next page": "下一页",
    "Get output files": "获取输出文件", "Send file": "发送文件", "Send to Slack": "发送到 Slack",
    "Output files (select one or more)": "输出文件（可多选）", "Local file path": "本地文件路径",
    "Output files referenced by this task (up to 20; current contents):": "本次任务引用的输出文件（最多 20 个，发送当前内容）：",
    "Choose output files OR enter one file path.": "请选择输出文件，或填写一个文件路径，两者选其一。",
    "Choose one or more files, then Send to Slack. No task is rerun.": "选择文件后发送到 Slack，不会重新执行任务。",
    "Select up to 20 output files OR enter one project file path. Sends current files to this Slack thread; never reruns the task.": "最多选择 20 个输出文件，或填写一个项目文件路径。发送当前文件到本会话线程，不会重跑任务。",
    "Recent files are loading. You can enter a project file path now. Retrieval never reruns the task.": "正在加载最近文件，也可填写项目文件路径。取回文件不会重跑任务。",
    "Recent files are unavailable. Enter a project file path; it will be revalidated before upload.": "最近文件暂不可用。可填写项目文件路径，上传前会重新核验。",
    "Some files could not be sent. Check !status before resending.": "部分文件未能发送，请先查看 !status 再决定是否重发。",
    "The file could not be sent. Check !status before resending.": "文件未能发送，请先查看 !status 再决定是否重发。",
    "Files were not resent. Check !status before trying again.": "没有重复发送文件，请先查看 !status。",
    "The file was not sent. Check !status before trying again.": "文件没有发送，请先查看 !status。",
    'Use !artifact "PATH" to retrieve one file.': '使用 !artifact "PATH" 取回一个文件。',
    'No explicit file references yet. Use !artifact "PATH". No task is rerun.': '暂无明确的文件引用。使用 !artifact "PATH" 取回文件，不会重跑任务。',
    'No matching output file from the latest task. Use !artifacts or !artifact "PATH". No task was rerun.': '最近任务中没有匹配的输出文件。使用 !artifacts 或 !artifact "PATH"，不会重跑任务。',
    "Click Take control on the session card before sending a new task.": "发送新任务前，请先点击会话卡片上的接管输入。",
    "This message arrived before control was acquired. Send a new message after taking control.": "此消息早于接管输入，请接管后重新发送。",
    "This chat is working. Use Steer to add instructions to its current task.": "会话正在执行任务，可用追加指令补充当前任务。",
    "Use the latest session card; this control has expired.": "此控件已过期，请使用最新会话卡片。",
    "This message is too old or its timestamp is invalid. Send a new message.": "消息过旧或时间戳无效，请发送新消息。",
    "Resolve the pending approval or wait for compaction before sending a new task.": "请先处理待审批请求，或等待上下文压缩完成。",
    "The chat is reconnecting. Refresh its session card before sending a new task.": "会话正在重连，请刷新卡片后再发送任务。",
    "An earlier outcome needs review. Check !status before sending another task.": "有历史操作结果待核查，请先查看 !status。",
    "Another task is using this project. Wait for it to finish.": "其他任务正在使用此项目，请等待完成。",
    "A request is still being submitted. Wait before sending another task.": "请求正在提交，请稍后再发送任务。",
    "Delivery could not be confirmed. Do not resend; check !status and the original client.": "无法确认是否送达，请勿重复发送，先检查 !status 和原客户端。",
    "The request could not be submitted. Check !status for details.": "请求未能提交，详情请查看 !status。",
    "This selection expired or was already used. Send !tasks for a fresh chat list or !project to select a project. No task was submitted.": "此选项已过期或已使用。发送 !tasks 刷新会话列表，或 !project 选择项目。未提交任务。",
    "Context remaining: unknown (waiting for native usage telemetry).": "剩余上下文：未知（等待原生用量遥测）。",
    "Send tasks and retrieve files in this thread. For this connection, use the Codex desktop for Steer, approvals, model/Skills settings, Compact and Fork.": "在本线程发送任务和取回文件。此连接的追加指令、审批、模型/技能设置、压缩和分叉请在 Codex 桌面操作。",
    "Session settings. Write ordinary tasks directly in this reply thread. File retrieval: !artifacts or ask to send a known output file. Compact and Fork are explicit operations; input ownership and approvals still apply.": "会话设置。普通任务直接回复本线程；取回文件可用 !artifacts 或直接要求发送已有输出文件。压缩和分叉需要显式操作，输入权限和审批仍然生效。",
    "Select a project with !project, then Open chat and use the session card's replies. Send tasks and attachments only inside that session's reply thread. No task was submitted.": "先用 !project 选择项目并打开会话，再点击卡片的回复数进入线程。任务和附件请发送到该会话的回复线程。未提交任务。",
    "This chat is still running a task. Wait for it to finish, then click Take control again.": "会话仍有任务运行，请等待完成后再次接管输入。",
    "This control is outdated. Use the latest session card and click Take control again.": "此控件已过期，请在最新会话卡片上重新接管输入。",
    "Resolve the pending approval before changing input mode.": "请先处理待审批请求，再更改输入模式。",
    "Wait for context compaction to finish before changing input mode.": "请等待上下文压缩完成，再更改输入模式。",
    "Review the protected unknown operation before changing input mode.": "请先核查受保护的未知操作，再更改输入模式。",
    "A submission is still in progress. Wait for its receipt before changing input mode.": "仍有请求正在提交，请等待回执后再更改输入模式。",
    "The chat is not synchronized. Refresh its session card before trying again.": "会话未同步，请刷新卡片后再试。",
    "Refresh the session card and check its status before trying again.": "请刷新会话卡片并检查状态后再试。",
    "This root is unavailable. Use !roots in this DM for explicit recovery.": "此会话根消息不可用，请在本私信中使用 !roots 恢复。",
    "Bound Codex chat; desktop input is the default. Use this thread.": "已绑定 Codex 会话，默认使用桌面输入。请在本线程继续。",
    "Select model": "选择模型", "Reasoning effort": "推理强度", "Skills": "技能",
    "Compact": "压缩上下文", "Fork": "分叉会话", "Steer": "追加指令",
    "Steer active turn": "向当前任务追加指令", "Compose task": "编写任务", "Send once": "发送一次",
    "Use skill": "使用技能", "Use selected skill": "使用所选技能", "Details": "详情",
    "Save for next task": "保存供下次任务使用", "Answer request": "回复请求", "your answer": "你的回答",
    "Skill name or exact path (optional; see Skills)": "技能名称或精确路径（可选，参见技能）",
    "Local file paths in quotes (optional; up to 5)": "用引号包围的本地文件路径（可选，最多 5 个）",
    "Loading current model options. No setting has been changed.": "正在加载模型选项，尚未更改设置。",
    "Model selection uses its default effort; use Reasoning effort to refine it.": "选择模型会使用其默认强度，可在推理强度中调整。",
    "Only efforts supported by this model are offered.": "只显示此模型支持的推理强度。",
    "Risk confirmation": "风险确认", "What did you check?": "已核查哪些内容？",
    "I checked the original and accept the remaining uncertainty": "我已检查原客户端并接受剩余的不确定性",
    "Acknowledge unknown": "确认未知结果", "Release this block": "解除此操作的阻塞",
    "Unknown operation review": "未知操作核查", "Submit NEW request": "提交新请求",
    "Review this block": "核查此阻塞", "New request, duplicate risk": "新请求：存在重复执行风险",
    "The original may already have executed. Create one NEW request?": "原请求可能已执行，是否创建一个新请求？",
    "Submit new": "提交新请求", "Create replacement root": "创建替代根消息",
    "Replace this root?": "替换此根消息？", "Replace root": "替换根消息",
    "Invalidate old controls and attempt one new root? An unconfirmed root may already exist.": "使旧控件失效并尝试创建一个新根消息？可能已有结果未确认的根消息。",
    "Root preview is too large to display completely. Inspect locally; no replacement action is available.": "根消息预览过大，请在本地检查，当前不提供替换操作。",
    "Request closed, expired, sent without a receipt, or invalidated. No remote decision is available.": "请求已关闭、过期、失效或发送后无回执，无法远程操作。",
    "Binding not found in this configured DM.": "在配置的私信中未找到绑定。",
    "Root creation is unconfirmed; a second root will not be posted. Check local state.": "根消息创建结果未确认，不会重复创建，请检查本地状态。",
    "Model": "模型", "Previous": "上一页", "Next": "下一页", "Task": "任务",
    "Project:": "项目：", "all projects": "全部项目",
    "Use !tasks to refresh or !project to change project.": "使用 !tasks 刷新，或 !project 更换项目。",
    "Project access not enabled. Enable this exact project before binding.": "项目尚未启用访问权限，请先允许此项目目录再绑定。",
    "Reply in this thread to continue this session. Settings contains skills and file retrieval.": "回复本线程即可继续会话，设置中包含技能和文件取回。",
    "For this connection, use Codex desktop for Steer, approvals and model/Skills settings.": "此连接的追加指令、审批和模型/技能设置请在 Codex 桌面操作。",
    "Progress:": "进展：",
    "Status: syncing/offline | unknown": "状态：同步中/离线 | 状态未知",
    "No fresh native status. Open this chat in Codex desktop, then Refresh.": "暂无新的原生状态。请在 Codex 桌面打开此会话，再点击刷新。",
    "Context compaction pending; new tasks are protected.": "上下文正在压缩，新任务暂受保护。",
    "Unknown / semantic mismatch: protected; inspect operation receipts.": "存在未知结果或语义不匹配，已启用保护，请核查操作回执。",
    "Account limits: unavailable (no windows inferred).": "账户额度：暂不可用（未推测额度窗口）。",
    "Account limits (shared account; Asia/Singapore):": "账户额度（共享账户；新加坡时间）：",
    "This session has pending Slack attachments. Send your instruction in this reply thread; use !skill EXACT_PATH -- task to include a chosen skill. No task was submitted from this form.": "会话有待使用的 Slack 附件，请在本线程发送指令。可用 !skill EXACT_PATH -- task 指定技能。本表单未提交任务。",
    "This session has pending Slack attachments. Send a plain task instruction in this thread to use them; local-path input cannot silently replace that context.": "会话有待使用的 Slack 附件，请在本线程直接发送任务指令。本地路径输入不能替换这些附件。",
    "Check the original client and actual effects. Acknowledgement releases only this record's block; it leaves the outcome unknown and does not resend or change input ownership.": "请检查原客户端和实际效果。确认仅解除此记录的阻塞，结果仍为未知，不会重发或改变输入权限。",
}

# Anchored bridge templates preserve all variable values, including native names and paths.
TEMPLATES = [
    (r"Model: (.+?) \| Effort: (.+)", "模型：{0} | 推理强度：{1}"),
    (r"Status: (online|syncing/offline) \| (working|idle|unknown|confirming completion|waiting for approval|compacting)", "状态：{0} | {1}"),
    (r"Input: (slack|desktop|suspended) \(GUI is not locked\) \| Approval pending: (yes|no|unknown)", "输入：{0}（桌面未锁定） | 待审批：{1}"),
    (r"Chat history ready\. Chat: (.+)\. Live status unavailable; open this chat in Codex desktop, then Refresh\. No task was submitted\.", "会话历史记录已就绪。会话：{0}。原生状态暂不可用，请在 Codex 桌面打开此会话后刷新。未提交任务。"),
    (r"Desktop (\d+) \| Existing desktop IPC — loaded chats; native processes stay running", "桌面 {0} | 现有桌面 IPC — 已加载会话，原生进程保持运行"),
    (r"Desktop (\d+) \| backend (\d+), port (\d+) — ready to confirm", "桌面 {0} | 后端 {1}，端口 {2} — 等待确认"),
    (r"Skills — (\d+) matches\. Page (\d+)\. Filter: !skills TEXT\.", "技能 — 匹配 {0} 项，第 {1} 页。筛选：!skills TEXT。"),
    (r"Received (\d+) attachment\(s\) for this session\. No task started\. Send your instruction in this thread within 10 minutes\. Context is used once; reconnect/restart requires sending the files again\.", "本会话已收到 {0} 个附件，尚未启动任务。请在 10 分钟内在本线程发送指令。附件只使用一次，重连或重启后需要重新发送。"),
    (r"Attachment input did not complete: (.+?)\. No partial text-only task was submitted\. Inspect !status / operation receipts before retrying; resend all required files with your instruction\.", "附件输入未完成：{0}。没有提交缺少附件的纯文本任务。请先检查 !status 和操作回执，再连同指令重新发送所需文件。"),
    (r"File retrieval did not complete: (.+?)\. No task was rerun\. An unconfirmed upload is never automatically repeated\.", "文件取回未完成：{0}。未重跑任务，也不会自动重试结果未确认的上传。"),
    (r"Task query failed: (.+?)\. No task submitted\.", "会话查询失败：{0}。未提交任务。"),
    (r"Next settings: (.+)", "下次任务设置：{0}"),
    (r"Query blocking operations: (.+)", "查询阻塞操作：{0}"),
    (r"Next-turn effort options: (.+)\. Use !effort VALUE\.", "下次任务可用推理强度：{0}。使用 !effort VALUE。"),
    (r"Risk acknowledged; the outcome remains unknown\. Use !replace (.+) to preview a new request\.", "已确认风险，结果仍然未知。使用 !replace {0} 预览新请求。"),
    (r"Commands: (.+)", "命令：{0}"),
    (r"Send output file to this Slack thread\?\n(.+)", "是否发送此文件到本 Slack 会话线程？\n{0}"),
    (r"Output unavailable: (.+)", "输出文件不可用：{0}"),
    (r"Task (completed|failed|interrupted)\.", "任务{0}。"),
    (r"Task (.+); no text reply\.", "任务状态：{0}，没有文本回复。"),
    (r"Chat controls ready\. Chat: (.+)\. Open this Slack thread to continue\. No task was submitted\.", "会话控件已就绪。会话：{0}。打开本 Slack 线程继续，未提交任务。"),
    (r"Choose a project\. (\d+) projects; page (.+)", "请选择项目，共 {0} 个，第 {1}"),
    (r"Fresh non-archived chats: (\d+); page (.+?)\. Project: (.+?)\. Use !tasks to refresh or !project to change project\.", "非归档会话：{0} 个，第 {1} 页。项目：{2}。使用 !tasks 刷新，或 !project 更换项目。"),
    (r"Fresh non-archived chats: (\d+); page (\d+)\.", "非归档会话：{0} 个，第 {1} 页。"),
    (r"Current model: (.+)", "当前模型：{0}"),
    (r"Selected model: (.+?)\. Changes apply to the next task only\. (.+)", "所选模型：{0}。仅对下次任务生效。{1}"),
    (r"Context remaining: (.+)", "剩余上下文：{0}"),
    (r"Model: (.+)", "模型：{0}"), (r"Status: (.+)", "状态：{0}"), (r"Input: (.+)", "输入：{0}"),
    (r"State: (.+)", "状态：{0}"), (r"Desktop/backend identity: (.+)", "桌面/后端身份：{0}"),
    (r"Bridge version: (.+)", "桥接版本：{0}"), (r"Bridge process: (.+)", "桥接进程：{0}"),
    (r"Backend: (.+)", "后端：{0}"), (r"Slack: (.+)", "Slack：{0}"),
    (r"Recovery attempts: (.+)", "恢复次数：{0}"), (r"Recent code: (.+)", "最近诊断代码：{0}"),
    (r"Last action error: (.+)", "最近操作错误：{0}"),
    (r"Account limits: (.+)", "账户额度：{0}"),
]

STATES = {"running": "运行中", "stopped": "已停止", "connecting": "连接中", "suspended": "已暂停",
    "verified": "已核验", "unverified": "未核验", "connected": "已连接", "disconnected": "未连接",
    "ready": "就绪", "not ready": "未就绪", "unknown": "未知", "attention required": "需要处理",
    "recovery backoff": "等待恢复", "external worker (observe only)": "外部进程（仅观察）",
    "choose an existing instance": "请选择现有实例", "no running desktop detected": "未发现运行中的桌面",
    "completed": "已完成", "failed": "失败", "interrupted": "已中断", "unconfirmed": "未确认",
    "online": "在线", "syncing/offline": "同步中/离线", "working": "工作中", "idle": "空闲",
    "desktop": "桌面", "slack": "Slack", "yes": "是", "no": "否",
    "confirming completion": "正在确认完成", "waiting for approval": "等待审批", "compacting": "压缩上下文中"}


def translate(text, language):
    if language != "zh-CN" or not isinstance(text, str) or isinstance(text, Literal):
        return text
    if text in ZH:
        return ZH[text]
    instance = re.fullmatch(r"Desktop (\d+) \| (.+)", text)
    if instance and instance[2] in ZH:
        return f"桌面 {instance[1]} | {ZH[instance[2]]}"
    if "\n" in text and text.startswith(("State: ", "Context remaining: ", "Account limits (")):
        return "\n".join(translate(line, language) for line in text.split("\n"))
    for pattern, template in TEMPLATES:
        match = re.fullmatch(pattern, text, re.S)
        if match:
            return template.format(*(STATES.get(value, value) for value in match.groups()))
    return text


def blocks(value, language):
    """Translate display strings, never values, IDs, URLs, options or Markdown."""
    if isinstance(value, list):
        return [blocks(item, language) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: (translate(item, language) if key == "text" and isinstance(item, str)
                 and value.get("type") == "plain_text" else
                 item if key in {"options", "initial_option", "initial_options"} else blocks(item, language))
            for key, item in value.items()}
