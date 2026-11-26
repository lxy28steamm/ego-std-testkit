import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from framework import livflow_flow


def test_logged_in_account_is_signed_out_before_login_validation():
    flow = livflow_flow.Flow.__new__(livflow_flow.Flow)
    flow.ui = Mock()
    flow.home = Mock()
    flow.note = Mock()
    flow.pause = Mock()
    flow.ui.text.side_effect = ['任务中心', '欢迎登录', '欢迎登录']
    flow.ui.wait.return_value = '任务中心'
    flow.ui.find.return_value = True

    flow.prepare_login_form()

    assert flow.ui.tap.call_args_list == [call('任务'), call('我的'), call('退出登录'), call('确认退出')]
    flow.pause.assert_not_called()


def test_configured_account_submits_the_app_login_form(monkeypatch):
    from xml.etree.ElementTree import fromstring

    monkeypatch.setenv('LIVFLOW_TEST_PHONE', '12345678901')
    monkeypatch.setenv('LIVFLOW_TEST_PASSWORD', 'test-password')
    monkeypatch.delenv('LIVFLOW_TEST_ROLE', raising=False)
    flow = livflow_flow.Flow.__new__(livflow_flow.Flow)
    flow.ui = Mock()
    flow.note = Mock()
    flow.pause = Mock()
    flow.ui.tree.return_value = fromstring(
        '<hierarchy><node class="android.widget.EditText"/><node class="android.widget.EditText"/></hierarchy>')
    flow.ui.wait.side_effect = lambda check, **_: '任务\n我的' if check('任务\n我的') else ''

    flow.sign_in()

    assert flow.ui.enter_login_value.call_args_list == [
        call('12345678901', password_field=False), call('test-password', password_field=True)]
    flow.ui.tap.assert_called_once_with('登录')
    flow.pause.assert_not_called()


def test_login_agreement_is_checked_before_submit(monkeypatch):
    from xml.etree.ElementTree import fromstring

    monkeypatch.setenv('LIVFLOW_TEST_PHONE', '12345678901')
    monkeypatch.setenv('LIVFLOW_TEST_PASSWORD', 'test-password')
    form = '<hierarchy><node class="android.widget.EditText"/><node class="android.widget.EditText"/>' \
           '<node class="android.widget.CheckBox" text="同意隐私协议" checkable="true" checked="{}"/></hierarchy>'
    flow = livflow_flow.Flow.__new__(livflow_flow.Flow)
    flow.ui = Mock()
    flow.note = Mock()
    flow.ui.tree.return_value = fromstring(form.format('false'))

    def wait(condition, *, tree_check=None, **kwargs):
        if tree_check:
            assert not tree_check(fromstring(form.format('false')))
            assert tree_check(fromstring(form.format('true')))
        assert condition('任务\n我的')
        return '任务\n我的'

    flow.ui.wait.side_effect = wait
    flow.sign_in(require_automatic=True)
    assert flow.ui.tap.call_args_list == [
        call('同意隐私协议', resource_id=None, class_name='android.widget.CheckBox'), call('登录')]


def setup_flow(tmp_path, monkeypatch, timeout=8):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(livflow_flow, 'time', SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=lambda seconds: setattr(clock, 'now', clock.now + seconds),
    ))
    flow = livflow_flow.Flow.__new__(livflow_flow.Flow)
    flow.output = tmp_path
    flow.config = {'human_timeout': timeout}
    flow.check_cancel = lambda: None
    flow.note = lambda message: None
    events = []
    monkeypatch.setattr(livflow_flow, 'event', lambda kind, **data: events.append((kind, data)))
    return flow, events


def test_ready_must_be_observed_twice_and_gate_is_closed(tmp_path, monkeypatch):
    flow, events = setup_flow(tmp_path, monkeypatch)
    observed = iter([False, True, False, True, True])
    flow.config['human_timeout'] = 12
    flow.pause('wait', ready=lambda: next(observed))
    assert events[0][0] == 'waiting'
    assert events[-2][1]['details']['feedback'] == '已连续两次读到所需状态，自动继续'
    assert events[-1][0] == 'resumed'


def test_unconfirmed_condition_times_out_without_success(tmp_path, monkeypatch):
    flow, events = setup_flow(tmp_path, monkeypatch, timeout=3)
    with pytest.raises(AssertionError, match='等待人工操作超时'):
        flow.pause('wait', ready=lambda: False)
    assert events[-2][1]['details']['feedback'] == '等待人工操作超时：wait'
    assert events[-1][0] == 'gate_closed'


def test_manual_resume_still_works_when_no_condition_is_available(tmp_path, monkeypatch):
    flow, events = setup_flow(tmp_path, monkeypatch)
    def on_event(kind, **data):
        events.append((kind, data))
        if kind == 'waiting':
            (tmp_path / 'resume.json').write_text(json.dumps({'gate': data['gate']}), encoding='utf-8')
    monkeypatch.setattr(livflow_flow, 'event', on_event)
    flow.pause('wait')
    assert events[-2][1]['details']['feedback'] == '已收到继续确认，开始后续检查'
    assert events[-1][0] == 'resumed'
