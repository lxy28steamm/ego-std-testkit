"""Read-only MCAP checks. Does not claim physical sensor synchronization."""
import hashlib
import json
from pathlib import Path
from mcap.reader import make_reader


def inspect(path):
    path = Path(path)
    digest = hashlib.sha256()
    topics = {}
    with path.open('rb') as stream:
        reader = make_reader(stream, validate_crcs=True)
        for schema, channel, msg in reader.iter_messages(log_time_order=False):
            row = topics.setdefault(channel.topic, dict(count=0, backward=0, first=msg.log_time,
                                                        last=msg.log_time, max_gap_ns=0))
            if row['count']:
                gap = msg.log_time-row['last']
                row['backward'] += int(gap<0)
                row['max_gap_ns'] = max(row['max_gap_ns'], gap)
            row['count'] += 1
            row['last'] = msg.log_time
            header = json.dumps([channel.topic, channel.message_encoding,
                schema.name if schema else None, msg.log_time,msg.publish_time,msg.sequence]).encode()
            for value in (header,msg.data):
                digest.update(len(value).to_bytes(8,'little'))
                digest.update(value)
        metadata = []
        for record in reader.iter_metadata():
            fields = record.metadata
            if record.name in ('livflow.camera_calibration.v1','livflow.imu_calibration.v1','livflow.capture_meta.v1'):
                filename = fields.get('filename','')
                assert filename and Path(filename).name == filename, '元数据引用了不安全的文件名'
                # Source calibration files live at the recording root, above part0001.
                source = path.parent.parent/filename
                content = fields.get('content','').encode('utf-8')
                metadata.append(dict(name=record.name, filename=filename, source_found=source.is_file(),
                    matches=source.is_file() and source.read_bytes()==content,
                    sha_matches=fields.get('sha256')==hashlib.sha256(content).hexdigest()))
    assert topics, 'MCAP 中没有传感器消息'
    return dict(file=path.name, bytes=path.stat().st_size, crc='PASS', topics=topics,
                message_digest=digest.hexdigest(), metadata=metadata)


def compare(before, after):
    a,b = inspect(before),inspect(after)
    expected = {'livflow.camera_calibration.v1','livflow.imu_calibration.v1','livflow.capture_meta.v1'}
    return dict(before=a, after=b, messages_unchanged=a['message_digest']==b['message_digest'],
                source_content_matches={m['name'] for m in b['metadata']}==expected
                    and all(m['matches'] and m['sha_matches'] for m in b['metadata']))
