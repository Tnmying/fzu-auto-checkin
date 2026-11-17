"""Run real failure branches with fake upstreams; never log in or send messages."""
import datetime
from unittest.mock import Mock

import pytest
import requests
import yaml

import main as entry
import src.alerts as alerts
import src.login as login
import src.notify as notifications
from src.schedule import TZ_CN


@pytest.fixture
def setup_run(monkeypatch, tmp_path):
    cfg = {
        "user": {"token": "private-token", "username": "student", "password": "private-password"},
        "checkin": {"longitude": "119.19", "latitude": "26.05"},
        "notify": {"type": "serverchan", "serverchan_key": "private-key"},
    }
    now = datetime.datetime(2026, 11, 17, 22, 10, tzinfo=TZ_CN)
    marker = tmp_path / "alert" / "sent"
    monkeypatch.delenv("CHECKIN_FORCE", raising=False)
    monkeypatch.setenv("CHECKIN_ALERT_STATE_PATH", str(marker))
    monkeypatch.setattr(entry, "load_config", lambda: cfg)
    monkeypatch.setattr(entry, "now_cn", lambda: now)
    monkeypatch.setattr(entry, "matched_range", lambda cfg: None)
    monkeypatch.setattr(entry, "AttnClient", lambda token: object())
    push = Mock(return_value=True)
    monkeypatch.setattr(alerts, "notify", push)
    checkin = Mock(return_value=True)
    monkeypatch.setattr(entry, "do_checkin", checkin)
    return cfg, now, marker, push, checkin


def test_login_failure_alerts_and_fails_action(setup_run, monkeypatch, capsys):
    cfg, _, marker, push, checkin = setup_run
    cfg["user"]["token"] = ""
    monkeypatch.setattr(login, "login", Mock(side_effect=RuntimeError("private-password")))
    query = Mock()
    monkeypatch.setattr(entry, "query_today_task", query)
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    assert "登录失败" in push.call_args.args[1]
    assert marker.exists()
    query.assert_not_called()
    checkin.assert_not_called()
    assert "private-password" not in capsys.readouterr().out


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError("https://example.test/?token=private-token", response=response)


@pytest.mark.parametrize("error", [
    RuntimeError("init 失败: token 已失效 private-token"),
    http_error(401), http_error(403), http_error(500),
    requests.Timeout("private-token"), requests.ConnectionError("private-token"),
    ValueError("invalid JSON private-token"),
])
def test_query_failures_alert_after_old_2145_cutoff(setup_run, monkeypatch, capsys, error):
    _, _, _, push, checkin = setup_run
    monkeypatch.setattr(entry, "query_today_task", Mock(side_effect=error))
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    assert "凭证或查询异常" in push.call_args.args[1]
    assert "更新 CONFIG_YAML" in push.call_args.args[2]
    assert "private-token" not in repr(push.call_args.args[1:])
    assert "private-token" not in capsys.readouterr().out
    checkin.assert_not_called()


def test_delivery_receipt_suppresses_same_day_but_not_next_day(setup_run):
    cfg, now, marker, push, _ = setup_run
    error = RuntimeError("expired")
    assert alerts.report_failure(cfg, "query", error, now)
    assert not alerts.report_failure(cfg, "login", error, now)
    assert push.call_count == 1
    assert marker.read_text() == "2026-11-17"
    assert alerts.report_failure(cfg, "query", error, now + datetime.timedelta(days=1))
    assert push.call_count == 2


@pytest.mark.parametrize("delivery", [False, requests.Timeout("private-key")])
def test_failed_push_is_retried_without_delivery_receipt(setup_run, delivery):
    cfg, now, marker, push, _ = setup_run
    if isinstance(delivery, Exception):
        push.side_effect = delivery
    else:
        push.return_value = delivery
    for _ in range(2):
        assert not alerts.report_failure(cfg, "query", RuntimeError(), now)
    assert not marker.exists()
    assert push.call_count == 2


def test_force_can_verify_notifications_despite_daily_receipt(setup_run):
    cfg, now, _, push, _ = setup_run
    alerts.report_failure(cfg, "query", RuntimeError(), now)
    alerts.report_failure(cfg, "query", RuntimeError(), now, force=True)
    assert push.call_count == 2


def test_already_signed_remains_silent(setup_run, monkeypatch):
    _, _, _, push, checkin = setup_run
    monkeypatch.setattr(entry, "query_today_task", lambda *a: {"need_checkin": False, "init": {}})
    entry.main()
    push.assert_not_called()
    checkin.assert_not_called()


@pytest.mark.parametrize("result", [False, RuntimeError("private-token")])
def test_submission_failures_are_red_in_actions(setup_run, monkeypatch, result):
    _, _, _, push, checkin = setup_run
    monkeypatch.setattr(entry, "query_today_task", lambda *a: {"need_checkin": True, "init": {"schoolData": [{}]}})
    if isinstance(result, Exception):
        checkin.side_effect = result
    else:
        checkin.return_value = result
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    push.assert_called_once()


def test_push_request_errors_do_not_print_sendkey(monkeypatch, capsys):
    monkeypatch.setattr(notifications.requests, "post", Mock(side_effect=requests.Timeout("https://sctapi.ftqq.com/private-key.send")))
    assert not notifications.notify({"notify": {"type": "serverchan", "serverchan_key": "private-key"}}, "test", "test")
    assert "private-key" not in capsys.readouterr().out


def test_workflow_restores_and_saves_receipt_even_after_failure():
    from pathlib import Path
    data = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/checkin.yml").read_text(encoding="utf-8"))
    steps = data["jobs"]["checkin"]["steps"]
    restore = next(s for s in steps if s.get("uses") == "actions/cache/restore@v4")
    save = next(s for s in steps if s.get("uses") == "actions/cache/save@v4")
    run = next(s for s in steps if s.get("name") == "Run checkin")
    assert restore["with"] == save["with"]
    assert "always()" in save["if"]
    assert run["env"]["CHECKIN_ALERT_STATE_PATH"] == save["with"]["path"]
