import os
import re
import secrets
from pathlib import Path

EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def generate_unique_code(length: int = 8) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def validate_email(email: str) -> bool:
    return bool(EMAIL_PATTERN.match((email or '').strip()))


def validate_phone(phone: str) -> bool:
    return bool(re.fullmatch(r'\+?\d{10,15}', (phone or '').strip()))


def sanitize_filename(name: str, max_length: int = 48) -> str:
    if not name:
        return "file"
    safe = os.path.basename(str(name))
    safe = re.sub(r"[^\w.-]", "_", safe, flags=re.UNICODE)
    return safe[:max_length] if safe else "file"


def get_user_storage_dir(base_dir: Path, user_id: int) -> Path:
    user_dir = base_dir / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def resolve_user_file_path(base_dir: Path, user_id: int, file_name: str) -> Path:
    user_dir = get_user_storage_dir(base_dir, user_id).resolve()
    safe_name = sanitize_filename(file_name)
    target = (user_dir / safe_name).resolve()
    if target.parent != user_dir:
        raise ValueError("Invalid file name")
    return target
