import json
from types import SimpleNamespace

import pytest
from framework import livflow_flow


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
    assert [event[0] for event in events] == ['waiting', 'resumed']


def test_unconfirmed_condition_times_out_without_success(tmp_path, monkeypatch):
    flow, events = setup_flow(tmp_path, monkeypatch, timeout=3)
    with pytest.raises(AssertionError, match='等待人工操作超时'):
        flow.pause('wait', ready=lambda: False)
    assert [event[0] for event in events] == ['waiting', 'resumed']


def test_manual_resume_still_works_when_no_condition_is_available(tmp_path, monkeypatch):
    flow, events = setup_flow(tmp_path, monkeypatch)
    def on_event(kind, **data):
        events.append((kind, data))
        if kind == 'waiting':
            (tmp_path / 'resume.json').write_text(json.dumps({'gate': data['gate']}), encoding='utf-8')
    monkeypatch.setattr(livflow_flow, 'event', on_event)
    flow.pause('wait')
    assert [event[0] for event in events] == ['waiting', 'resumed']
