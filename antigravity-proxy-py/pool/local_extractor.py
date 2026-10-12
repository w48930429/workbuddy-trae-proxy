import os
import json
import base64
import subprocess
from typing import Optional, Dict, Any


def extract_from_windows_credential() -> Optional[Dict[str, Any]]:
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ps_path = os.path.join(base_dir, 'scripts', 'read-cred.ps1')

    if not os.path.exists(ps_path):
        return None

    try:
        cmd = f'[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; & \'{ps_path}\''
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'

        result = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', cmd],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=env,
            timeout=15
        )

        output = result.stdout.strip()
        if not output:
            return None

        parsed = json.loads(output)
        token_obj = parsed.get('token') or {}

        email = 'antigravity-local@google.com'
        id_token = parsed.get('id_token')
        if id_token and isinstance(id_token, str):
            parts = id_token.split('.')
            if len(parts) >= 2:
                payload_part = parts[1]
                # Fix base64 padding
                rem = len(payload_part) % 4
                if rem > 0:
                    payload_part += '=' * (4 - rem)
                try:
                    payload_json = base64.b64decode(payload_part).decode('utf-8')
                    id_data = json.loads(payload_json)
                    if id_data.get('email'):
                        email = id_data['email']
                except Exception:
                    pass

        return {
            'email': email,
            'refreshToken': token_obj.get('refresh_token'),
            'accessToken': token_obj.get('access_token'),
            'expiry': token_obj.get('expiry')
        }
    except Exception as err:
        print(f"Failed to extract Windows Credential: {err}")
        return None
