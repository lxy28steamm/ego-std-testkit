"""Reusable functional flows. Physical actions pause with an explicit resume gate."""
import os
from pathlib import Path
import pytest
from framework.livflow_flow import Flow, Cancelled, configuration

pytestmark = pytest.mark.device


@pytest.fixture(scope='session')
def _flow(pytestconfig):
    path = os.environ.get('APP_PATROL_FLOW_CONFIG', str(Path(__file__).parents[1]/'config/patrol.json'))
    return Flow(configuration(path), os.environ.get('ANDROID_UDID',''), pytestconfig.artifact_dir)


@pytest.fixture
def flow(_flow, request):
    _flow.item = request.node
    if _flow.needs_attention:
        pytest.skip('前一步未能确认设备已安全停止，后续流程停止，请人工检查设备')
    try:
        _flow.check_cancel()
        yield _flow
    except Cancelled:
        reason = '用户停止执行；未完成项目不计为通过'
        if _flow.upload_started:
            reason += '。APP 上传可能仍在进行，请在手机确认，不要拔卡'
            _flow.note(reason)
        pytest.skip(reason)


def perform(flow, method):
    try:
        getattr(flow,method)()
    except Cancelled:
        reason='用户停止执行；未完成项目不计为通过'
        if flow.upload_started:
            reason+='。APP 上传可能仍在进行，请在手机确认，不要拔卡'
            flow.note(reason)
        pytest.skip(reason)
    except Exception:
        with flow.cleanup():
            try:
                flow.capture('failure')
            except Exception:
                pass
        raise


@pytest.mark.flow_tasks
def test_flow_search_and_reset(flow):
    perform(flow,'search_reset')


@pytest.mark.flow_tasks
def test_flow_filters(flow):
    perform(flow,'filters')


@pytest.mark.flow_tasks
def test_flow_task_statistics(flow):
    perform(flow,'detail')


@pytest.mark.flow_settings
def test_flow_language_restore(flow):
    perform(flow,'language')


@pytest.mark.flow_settings
def test_flow_about_and_log_export(flow):
    perform(flow,'about_logs')


@pytest.mark.flow_connect
def test_flow_target_connection(flow):
    perform(flow,'connect')


@pytest.mark.flow_parameters
def test_flow_parameter_write_restore(flow):
    if os.environ.get('APP_PATROL_ALLOW_PARAMETERS') != '1':
        pytest.skip('本轮未授权临时修改参数并恢复')
    perform(flow,'parameters')


@pytest.mark.flow_record
def test_flow_short_recording(flow):
    if os.environ.get('APP_PATROL_ALLOW_RECORD') != '1':
        pytest.skip('本轮未授权开始实际采集')
    perform(flow,'record')


@pytest.mark.flow_upload
def test_flow_upload_and_duplicate_guard(flow):
    if os.environ.get('APP_PATROL_ALLOW_UPLOAD') != '1':
        pytest.skip('本轮未授权写入测试MCAP并上传')
    perform(flow,'upload')
