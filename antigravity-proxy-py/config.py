import os
import json
from typing import Dict, Any

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

# Google OAuth credentials are read from the environment only. They are never
# compiled into the source or written to config.json: this repository is public,
# and a committed client secret is a leaked client secret even after it is
# removed from a later commit.
OAUTH_ENV_KEYS = ('oauthClientId', 'oauthClientSecret')

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
    'oauthClientId': os.environ.get('OAUTH_CLIENT_ID', ''),
    'oauthClientSecret': os.environ.get('OAUTH_CLIENT_SECRET', ''),
    'defaultProxyUrl': os.environ.get('DEFAULT_PROXY_URL', 'http://127.0.0.1:20081')
}


def oauth_config_from_env() -> Dict[str, str]:
    """Return the OAuth pair from the environment, never from a file."""
    return {'oauthClientId': os.environ.get('OAUTH_CLIENT_ID', ''),
            'oauthClientSecret': os.environ.get('OAUTH_CLIENT_SECRET', '')}


def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                cfg = dict(DEFAULT_CONFIG)
                cfg.update(data)
                # Never trust a config file for credentials. Older installs
                # stored the secret there; drop it on read and rewrite the
                # sanitised file so it does not keep leaking on disk.
                stripped = False
                for key in OAUTH_ENV_KEYS:
                    if key in data:
                        data.pop(key, None)
                        stripped = True
                cfg.update(oauth_config_from_env())
                if stripped:
                    save_config(cfg)
                return cfg
        except Exception as e:
            print(f"Failed to parse config.json, using defaults: {e}")
    else:
        save_config(DEFAULT_CONFIG)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(oauth_config_from_env())
    return cfg


def save_config(config: Dict[str, Any]) -> None:
    try:
        # Credentials are environment-only; keep them out of the file even if
        # a caller puts them in the dict.
        clean = {k: v for k, v in config.items() if k not in OAUTH_ENV_KEYS}
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(clean, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Failed to save config.json: {e}")
