"""谁可以在这一步被点名。平台是唯一调度者。"""

AGENTS = ("codex", "doubao", "grok", "workbuddy")

LABELS = {
    "codex": "Codex",
    "doubao": "豆包",
    "grok": "Grok",
    "workbuddy": "WorkBuddy",
}

PHASE_LABELS = {
    "collect": "收数据",
    "analyze": "拆解核对",
    "draft": "写稿",
    "publish": "发布",
    "done": "已完成",
}

ARTIFACTS = {
    "codex": "research.json",
    "doubao": "douyin/analysis.md",
    "grok": "review.md",
    "workbuddy": "wechat/article.md",
}

PUBLISH_ARTIFACT = "douyin/publish-pack/status.md"


class ControlError(Exception):
    pass


def replied(job, agent, *, publish=None):
    for message in job["messages"]:
        if message["role"] != agent or message["kind"] != "reply":
            continue
        if publish is None or bool(message.get("publish")) == publish:
            return True
    return False


def actions(job):
    running = job.get("running") is not None
    phase = job["phase"]
    call = []
    if not running:
        if phase == "collect":
            call = ["codex"]
        elif phase == "analyze":
            call = ["codex", "doubao", "grok"]
        elif phase == "draft":
            call = ["codex", "doubao", "grok", "workbuddy"]
        elif phase == "publish" and not replied(job, "doubao", publish=True):
            call = ["doubao"]
    return {
        "start": phase == "collect" and not running and not replied(job, "codex"),
        "call": call,
        "stop": running,
        "confirm_topic": (
            phase == "analyze"
            and not running
            and replied(job, "doubao")
            and replied(job, "grok")
        ),
        "confirm_publish": (
            phase == "draft" and not running and replied(job, "workbuddy")
        ),
    }


def assert_can_call(job, agent):
    if agent not in AGENTS:
        raise ControlError(f"未知代理：{agent}")
    allowed = actions(job)["call"]
    if agent not in allowed:
        phase = PHASE_LABELS.get(job["phase"], job["phase"])
        if job.get("running"):
            who = LABELS[job["running"]["agent"]]
            raise ControlError(f"现在正在听{who}说，不能再叫人")
        raise ControlError(f"「{phase}」这一步不能叫{LABELS[agent]}")


def assert_can_confirm_topic(job):
    if not actions(job)["confirm_topic"]:
        raise ControlError("选题还不能确认：要等豆包和 Grok 都说过一次")


def assert_can_confirm_publish(job):
    if not actions(job)["confirm_publish"]:
        raise ControlError("发布还不能确认：要等 WorkBuddy 把公众号稿写完")


def is_publish_turn(job, agent):
    return job["phase"] == "publish" and agent == "doubao"


def artifact_for(agent, publish):
    if agent == "doubao" and publish:
        return PUBLISH_ARTIFACT
    return ARTIFACTS[agent]


def advance_after_reply(job, agent, publish):
    if job["phase"] == "collect" and agent == "codex":
        job["phase"] = "analyze"
    elif publish and agent == "doubao":
        job["phase"] = "done"
