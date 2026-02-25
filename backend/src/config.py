import os


def get_service_key(header_value: str | None = None) -> str:
    """Return the data.go.kr service key.

    Priority:
    1. Environment variable DATA_GO_KR_SERVICE_KEY
    2. Value passed from request header X-DATA-GO-KR-KEY
    """
    key = os.environ.get("DATA_GO_KR_SERVICE_KEY")
    if key:
        return key
    if header_value:
        return header_value
    raise ValueError(
        "No API key found. Set DATA_GO_KR_SERVICE_KEY env var or pass X-DATA-GO-KR-KEY header."
    )
