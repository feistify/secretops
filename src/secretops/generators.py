import secrets
import string
from typing import Any

SAFE_PASSWORD_CHARS = string.ascii_letters + string.digits + "_- .".replace(" ", "")


def generate_secret_value(generator: dict[str, Any]) -> str:
    generator_type = generator.get("type")
    if generator_type != "password":
        raise ValueError(f"unsupported generator type: {generator_type}")

    length = int(generator.get("length", 32))
    if length <= 0:
        raise ValueError("generator length must be positive")

    return "".join(secrets.choice(SAFE_PASSWORD_CHARS) for _ in range(length))
