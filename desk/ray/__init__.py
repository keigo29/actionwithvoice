"""デスク常駐型の音声アシスタント「Ray（レイ）」"""

import os as _os

# python.org の Python は Mac の証明書を使わないので、HTTPS（天気・Gemini）が
# 「CERTIFICATE_VERIFY_FAILED」で失敗する。certifi の証明書を使わせる（自分で設定していればそれを優先）
try:
    import certifi as _certifi

    _os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
except ImportError:
    pass
