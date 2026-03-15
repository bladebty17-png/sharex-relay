import asyncio
import websockets
import json
import time
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('BlayzRelay')

# All connected clients: device_id -> websocket
clients = {}

# Active sessions: session_id -> {host_id, viewer_id, type}
sessions = {}

async def send_json(ws, data):
    try:
        await ws.send(json.dumps(data))
    except Exception:
        pass

async def handle_client(websocket):
    device_id = None
    try:
        # Step 1 — Register
        raw = await asyncio.wait_for(websocket.recv(), timeout=10)
        msg = json.loads(raw)

        if msg.get('type') != 'register':
            await send_json(websocket, {'type': 'error', 'msg': 'Must register first'})
            return

        device_id = msg.get('device_id', '').upper().strip()
        username  = msg.get('username', 'Unknown')
        client_type = msg.get('client_type', 'pc')  # pc / mobile

        if not device_id:
            await send_json(websocket, {'type': 'error', 'msg': 'No device_id'})
            return

        clients[device_id] = {
            'ws':          websocket,
            'username':    username,
            'device_id':   device_id,
            'client_type': client_type,
            'connected_at': time.time(),
        }

        await send_json(websocket, {
            'type':      'registered',
            'device_id': device_id,
            'msg':       f'Connected as {username}'
        })
        log.info(f"[+] {username} ({device_id}) [{client_type}] connected")

        # Step 2 — Message loop
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                await route_message(device_id, msg, websocket)
            except json.JSONDecodeError:
                pass

    except asyncio.TimeoutError:
        log.warning("Client timed out during registration")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if device_id and device_id in clients:
            del clients[device_id]
            log.info(f"[-] {device_id} disconnected")
            await notify_disconnect(device_id)


async def route_message(sender_id, msg, sender_ws):
    t = msg.get('type')

    # ── Connect request: Viewer wants to connect to Host ──
    if t == 'connect_request':
        target_id = msg.get('target_id', '').upper().strip()
        if target_id not in clients:
            await send_json(sender_ws, {'type': 'error', 'msg': 'Device not online'})
            return
        target = clients[target_id]
        sender = clients.get(sender_id, {})
        await send_json(target['ws'], {
            'type':        'incoming_request',
            'from_id':     sender_id,
            'from_name':   sender.get('username', 'Unknown'),
            'client_type': sender.get('client_type', 'pc'),
        })
        log.info(f"[~] {sender_id} → {target_id} connection request")

    # ── Host responds: allow or deny ──
    elif t == 'response':
        target_id = msg.get('to_id', '').upper().strip()
        allowed   = msg.get('allowed', False)
        if target_id not in clients:
            return
        await send_json(clients[target_id]['ws'], {
            'type':    'connect_response',
            'allowed': allowed,
            'host_id': sender_id,
            'host_name': clients.get(sender_id, {}).get('username', 'Host'),
        })
        if allowed:
            session_id = f"{sender_id}_{target_id}"
            sessions[session_id] = {
                'host_id':   sender_id,
                'viewer_id': target_id,
                'started_at': time.time()
            }
            log.info(f"[✓] Session started: {sender_id} <-> {target_id}")

    # ── Screen frame: Host → Viewer(s) ──
    elif t == 'frame':
        # Forward to all viewers of this host
        for sid, sess in sessions.items():
            if sess['host_id'] == sender_id:
                viewer_id = sess['viewer_id']
                if viewer_id in clients:
                    await send_json(clients[viewer_id]['ws'], msg)

    # ── Input: Viewer → Host ──
    elif t in ('mouse_move', 'mouse_click', 'mouse_scroll', 'key_press', 'key_release'):
        # Forward to host
        for sid, sess in sessions.items():
            if sess['viewer_id'] == sender_id:
                host_id = sess['host_id']
                if host_id in clients:
                    await send_json(clients[host_id]['ws'], msg)

    # ── Monitor info: Host → Viewers ──
    elif t == 'monitor_info':
        for sid, sess in sessions.items():
            if sess['host_id'] == sender_id:
                viewer_id = sess['viewer_id']
                if viewer_id in clients:
                    await send_json(clients[viewer_id]['ws'], msg)

    # ── Disconnect session ──
    elif t == 'disconnect_session':
        await cleanup_session(sender_id)

    # ── Ping/pong ──
    elif t == 'ping':
        await send_json(sender_ws, {'type': 'pong', 'ts': time.time()})

    # ── List online devices (for dashboard) ──
    elif t == 'list_online':
        online = [
            {'device_id': did, 'username': c['username'], 'client_type': c['client_type']}
            for did, c in clients.items()
            if did != sender_id
        ]
        await send_json(sender_ws, {'type': 'online_list', 'devices': online})


async def cleanup_session(device_id):
    to_remove = []
    for sid, sess in sessions.items():
        if sess['host_id'] == device_id or sess['viewer_id'] == device_id:
            to_remove.append(sid)
            other_id = sess['viewer_id'] if sess['host_id'] == device_id else sess['host_id']
            if other_id in clients:
                await send_json(clients[other_id]['ws'], {'type': 'session_ended'})
    for sid in to_remove:
        del sessions[sid]
        log.info(f"[x] Session {sid} ended")


async def notify_disconnect(device_id):
    await cleanup_session(device_id)


async def health_check(path, request_headers):
    if path == '/health':
        return (200, [('Content-Type', 'text/plain')], b'Blayz Relay OK')
    if path == '/status':
        status = json.dumps({
            'clients':  len(clients),
            'sessions': len(sessions),
            'uptime':   'running'
        }).encode()
        return (200, [('Content-Type', 'application/json')], status)


async def main():
    port = int(os.environ.get('PORT', 8765))
    log.info(f"Blayz Relay Server starting on port {port}")
    log.info(f"Health: http://localhost:{port}/health")
    log.info(f"Status: http://localhost:{port}/status")

    async with websockets.serve(
        handle_client, '0.0.0.0', port,
        process_request=health_check,
        ping_interval=30,
        ping_timeout=10,
        max_size=10 * 1024 * 1024,  # 10MB max frame
    ):
        log.info(f"Relay running! ws://0.0.0.0:{port}")
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
