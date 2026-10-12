import os
import json
from typing import Dict, Any

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

DEFAULT_OAUTH_CLIENT_ID = '1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com'
DEFAULT_OAUTH_CLIENT_SECRET = ''

DEFAULT_CONFIG: Dict[str, Any] = {
    'port': int(os.environ.get('PORT', '43811')),
    'host': os.environ.get('HOST', '0.0.0.0'),
    'apiMasterKey': os.environ.get('API_MASTER_KEY', 'sk-antigravity'),
    'adminPassword': os.environ.get('ADMIN_PASSWORD', 'admin123'),
    'upstreamUrl': os.environ.get('UPSTREAM_URL', 'https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse'),
    'defaultMaxConcurrency': 5,
    'cooldownSeconds': 60,
    'tokenRefreshIntervalMs': 3 * 60 * 1000,
    'routingStrategy': os.environ.get('ROUTING_STRATEGY', 'zero-waste'),
    'oauthClientId': os.environ.get('OAUTH_CLIENT_ID', DEFAULT_OAUTH_CLIENT_ID),
    'oauthClientSecret': os.environ.get('OAUTH_CLIENT_SECRET', DEFAULT_OAUTH_CLIENT_SECRET),
    'defaultProxyUrl': os.environ.get('DEFAULT_PROXY_URL', 'http://127.0.0.1:20081')
}


def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                cfg = dict(DEFAULT_CONFIG)
                cfg.update(data)
                return cfg
        except Exception as e:
            print(f"Failed to parse config.json, using defaults: {e}")
    else:
        save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(config: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Failed to save config.json: {e}")
