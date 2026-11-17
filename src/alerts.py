"""Failure alerts without including credentials or upstream response bodies."""
import os
from pathlib import Path

import requests

from src.notify import notify


def report_failure(cfg, stage, error, now, *, force=False):
    """Notify once per Beijing day in scheduled runs; retry if delivery failed.

    The workflow caches only the delivery date, never config or credentials.
    Manual force runs bypass suppression so users can verify a repaired setup.
    """
    day = now.date().isoformat()
    state_path = os.environ.get("CHECKIN_ALERT_STATE_PATH", "").strip()
    marker = Path(state_path) if state_path else None
    title = {
        "login": "智汇福大晚点名：登录失败 ❌",
        "query": "智汇福大晚点名：凭证或查询异常 ❌",
        "checkin": "智汇福大晚点名：签到失败 ❌",
        "location": "智汇福大晚点名：不在签到范围 ❌",
    }[stage]
    # Never echo str(error): response bodies and request URLs may contain secrets.
    detail = type(error).__name__
    if isinstance(error, requests.HTTPError) and error.response is not None:
        detail += f" HTTP {error.response.status_code}"
    print(f"{title}（{detail}），本次未确认签到成功。")

    if marker is not None and not force:
        try:
            if marker.read_text(encoding="utf-8").strip() == day:
                print("今日已成功发送过异常提醒，本次不重复推送；Actions 仍标记失败。")
                return False
        except OSError:
            pass

    hints = {
        "login": "学号密码自动登录失败。请检查凭据、验证码或登录接口；可更新 CONFIG_YAML 中的 token。",
        "query": "今日状态查询失败：可能是 token 失效、网络或学校服务异常。请检查 Actions；确认凭证失效后更新 CONFIG_YAML 中的 token。",
        "checkin": "提交签到失败，请检查 Actions 并打开学校 App 确认。",
        "location": "配置坐标未命中签到范围，请核对实际位置的高德经纬度。",
    }
    content = (
        f"执行时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）\n"
        f"错误类型：{detail}\n{hints[stage]}\n"
        "本次未确认签到成功，请及时打开学校 App 查看并按需手动签到。\n"
        "自动运行每天最多发送一次此类异常提醒；请持续留意成功通知和学校签到状态。"
    )
    try:
        delivered = bool(notify(cfg, title, content))
    except Exception:
        delivered = False
    if not delivered:
        print("[alert] 异常提醒未确认送达，请检查推送配置、网络和额度；后续运行会重试提醒。")
        return False
    if marker is not None and not force:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(day, encoding="utf-8")
        except OSError:
            print("[alert] 提醒已发送，但去重状态保存失败，后续运行可能重复提醒。")
    return True
