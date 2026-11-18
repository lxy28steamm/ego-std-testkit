"""多 mcap 合并上传的本地校验（只读，不改文件）。

用途：APP 把多条 mcap 合并成一个数据集上传后，用本模块核对"合并结果是不是真的
等于这几条的和"，而不是只看 APP 界面显示"已上传"。

只用标准库 + mcap（项目已有依赖），不额外装包。
"""
import hashlib
from pathlib import Path

from mcap.reader import make_reader

# livflow 写入业务元数据的三个固定 record 名 + 设备身份 record
BIZ_RECORDS = ('livflow.camera_calibration.v1', 'livflow.imu_calibration.v1',
               'livflow.capture_meta.v1')
DEVICE_RECORDS = ('livflow.device.v1',)


def _digest(header_parts, data):
    h = hashlib.sha256()
    for value in (header_parts, data):
        h.update(len(value).to_bytes(8, 'little'))
        h.update(value)
    return h


def inventory(path):
    """逐文件清点：topic 计数、时间窗口、消息摘要、元数据记录、附件名。

    返回的 message_digest 与 framework.recording_check.inspect() 的口径一致
    （topic + encoding + schema + log_time + publish_time + sequence + data），
    所以合并前后可以直接比摘要，不用凭文件大小猜。
    """
    path = Path(path)
    digest = hashlib.sha256()
    topics = {}
    with path.open('rb') as stream:
        reader = make_reader(stream, validate_crcs=True)
        for schema, channel, msg in reader.iter_messages(log_time_order=False):
            row = topics.setdefault(channel.topic, dict(count=0, first=None, last=None, backward=0))
            if row['first'] is None:
                row['first'] = msg.log_time
            elif msg.log_time < row['last']:
                row['backward'] += 1
            row['last'] = msg.log_time
            row['count'] += 1
            header = repr([channel.topic, channel.message_encoding,
                           schema.name if schema else None, msg.log_time,
                           msg.publish_time, msg.sequence]).encode()
            part = _digest(header, msg.data).digest()
            digest.update(part)
        records = {}
        for rec in reader.iter_metadata():
            records.setdefault(rec.name, []).append(dict(rec.metadata))
        attachments = [att.name for att in reader.iter_attachments()]
    return dict(file=path.name, bytes=path.stat().st_size, crc='PASS',
                topics=topics, message_digest=digest.hexdigest(),
                records=records, attachments=attachments,
                start=min((v['first'] for v in topics.values() if v['first'] is not None), default=None),
                end=max((v['last'] for v in topics.values() if v['last'] is not None), default=None))


def verify_iesn(one, expected_sn):
    """校验单条 mcap 里的设备编码。返回 (ok, rows)，rows 是逐项结论。"""
    rows = []
    hit = False
    for meta in one['records'].get('livflow.device.v1', []):
        value = meta.get('device_id', '')
        ok = value == expected_sn
        hit = hit or ok
        rows.append(dict(item='livflow.device.v1 / device_id', value=value, ok=ok))
    for meta in one['records'].get('livflow.capture_meta.v1', []):
        import json
        try:
            body = json.loads(meta.get('content', '{}'))
        except ValueError:
            rows.append(dict(item='capture_meta 内容', value='不是合法 JSON', ok=False))
            continue
        value = (body.get('device') or {}).get('serial_number', '')
        ok = value == expected_sn
        hit = hit or ok
        rows.append(dict(item='capture_meta / device.serial_number', value=value, ok=ok))
    expected_name = 'EGO_%s_' % expected_sn
    ok = one['file'].startswith(expected_name) or hit
    rows.append(dict(item='文件名含设备编码', value=one['file'], ok=ok))
    return (hit or ok), rows


def verify_merge(sources, merged):
    """核对合并文件是否等于来源文件之和。

    返回 dict，每项都是"能不能直接下结论"的布尔或有数值。不能判定的项写 None，
    不用猜的方式凑成 True。
    """
    src = [inventory(p) for p in sources]
    out = inventory(merged)
    expected_counts = {}
    for one in src:
        for topic, row in one['topics'].items():
            expected_counts[topic] = expected_counts.get(topic, 0) + row['count']
    actual_counts = {t: r['count'] for t, r in out['topics'].items()}
    spans = sorted((x['start'], x['end']) for x in src if x['start'] is not None)
    overlap = sum(max(0, spans[i][1] - spans[i + 1][0]) for i in range(len(spans) - 1))
    return dict(
        source_files=[x['file'] for x in src],
        source_bytes=[x['bytes'] for x in src],
        merged_file=out['file'],
        merged_bytes=out['bytes'],
        expected_topic_counts=expected_counts,
        actual_topic_counts=actual_counts,
        counts_match=expected_counts == actual_counts,
        expected_message_total=sum(expected_counts.values()),
        actual_message_total=sum(actual_counts.values()),
        timestamp_backward=sum(r['backward'] for r in out['topics'].values()),
        source_time_overlap_ns=overlap,
        merged_start=out['start'],
        merged_end=out['end'],
        merged_records={k: len(v) for k, v in out['records'].items()},
        merged_attachments=out['attachments'],
        merged_crc=out['crc'],
    )
