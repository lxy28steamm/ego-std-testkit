"""Run with aiortc/av in the device runtime, or a local environment containing those packages."""
import asyncio
import base64
import io
import json
import urllib.request
from aiortc import RTCPeerConnection,RTCConfiguration,RTCSessionDescription,RTCRtpSender

async def probe(base,stream_id):
    peer=RTCPeerConnection(RTCConfiguration(iceServers=[]));received=asyncio.get_running_loop().create_future();session=None
    def request(path,body=None,method=None):
        req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,method=method or ('POST' if body is not None else 'GET'),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=20) as r:
            return json.load(r) if r.status!=204 else None
    @peer.on('track')
    def track(t):
        async def read():
            try:
                frames=[];image=None
                for _ in range(3):
                    frame=await t.recv();frames.append({'width':frame.width,'height':frame.height,'pts':frame.pts})
                    image=frame
                data=image.to_ndarray(format='gray')
                result={'stream_id':stream_id,'frames':frames,'luma_mean':float(data.mean()),'luma_std':float(data.std())}
                try:
                    buf=io.BytesIO();image.to_image().save(buf,format='JPEG');result['jpeg_base64']=base64.b64encode(buf.getvalue()).decode()
                except Exception:pass
                if not received.done():received.set_result(result)
            except Exception as e:
                if not received.done():received.set_exception(e)
        asyncio.create_task(read())
    try:
        tx=peer.addTransceiver('video',direction='recvonly')
        tx.setCodecPreferences([c for c in RTCRtpSender.getCapabilities('video').codecs if c.mimeType.lower()=='video/h264'])
        await peer.setLocalDescription(await peer.createOffer())
        answer=await asyncio.to_thread(request,'/api/v2/preview/webrtc',{'sdp':peer.localDescription.sdp,'type':'offer','stream_id':stream_id})
        session=answer['session_id']
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'],type=answer['type']))
        return await asyncio.wait_for(received,25)
    finally:
        await peer.close()
        if session:
            try:await asyncio.to_thread(request,'/api/v2/preview/webrtc/'+session,None,'DELETE')
            except Exception:pass

if __name__=='__main__':
    import sys
    try:result=asyncio.run(probe(sys.argv[1],sys.argv[2]))
    except Exception as e:result={'error_type':type(e).__name__}
    print(json.dumps(result))
