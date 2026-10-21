"""Structured pytest events consumed by the workstation bridge."""
import json
import pytest

PREFIX = '@@APP_PATROL@@'
NAMES = {
    'test_flow_search_and_reset': '任务搜索与重置',
    'test_flow_filters': '任务状态及时间筛选',
    'test_flow_task_statistics': '指定任务详情及统计刷新',
    'test_flow_language_restore': '切换语言并恢复中文',
    'test_flow_about_and_log_export': '版本核对及日志导出',
    'test_flow_target_connection': '指定SN连接与配网预览',
    'test_flow_parameter_write_restore': 'IMU配置修改并恢复',
    'test_flow_short_recording': '短采集、后台返回及结束',
    'test_flow_upload_and_duplicate_guard': '测试文件备份、写入、上传、防重复',
    'test_recorded_mcap_integrity': 'MCAP完整性、时间戳及元数据比较',
    'test_reject_implicit_device_selection': '禁止自动选错设备',
    'test_environment_overrides': '环境配置覆盖',
    'test_missing_app_rejected': '缺少APP配置时拦截',
    'test_empty_login_validation': '登录空表单校验',
    'test_guest_navigation': '游客页面导航',
    'test_language_switch': '中英文切换与恢复',
    'test_phone_connected_to_open_wifi': '手机开放网络连接',
    'test_stereo_preview': '双目视频预览状态',
    'test_audio_and_imu_status': '音频与IMU收流状态',
}


def emit(kind, **values):
    print(PREFIX + json.dumps(dict(type=kind, **values), ensure_ascii=False), flush=True)


def case(item):
    name = item.originalname or item.name.split('[')[0]
    title = NAMES.get(name, name)
    if hasattr(item, 'callspec') and 'tab' in item.callspec.params:
        title += '：' + item.callspec.params['tab']
    return dict(id=item.nodeid, module='自动检查', title=title, status='未执行', detail='')


def pytest_collection_finish(session):
    emit('collected', cases=[case(item) for item in session.items])


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    reports = getattr(item, '_patrol_reports', [])
    reports.append(report)
    item._patrol_reports = reports
    if report.when == 'teardown':
        result = case(item)
        failed = next((r for r in reports if r.failed), None)
        skipped = next((r for r in reports if r.skipped), None)
        result['status'] = '失败' if failed else '未测' if skipped else '通过'
        result['detail'] = str(failed.longrepr)[-800:] if failed else str(skipped.longrepr) if skipped else '自动断言通过'
        observations = getattr(item,'patrol_observations',[])
        if observations and not failed and not skipped:
            result['status'] = '待确认'
            result['detail'] = '；'.join(observations)
        emit('case', case=result)
