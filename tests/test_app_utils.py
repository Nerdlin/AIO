from pathlib import Path

from app_utils import (
    generate_unique_code,
    validate_email,
    validate_phone,
    sanitize_filename,
    get_user_storage_dir,
    resolve_user_file_path,
)


def test_generate_unique_code_format():
    code = generate_unique_code()
    assert len(code) == 8
    assert code.isalnum()
    assert code.upper() == code


def test_email_and_phone_validators():
    assert validate_email('user@example.com')
    assert not validate_email('invalid-email')

    assert validate_phone('+77001234567')
    assert validate_phone('77001234567')
    assert not validate_phone('12345')


def test_sanitize_filename_and_length():
    safe_name = sanitize_filename('../../very*bad?name.pdf')
    assert safe_name.endswith('.pdf')
    assert '..' not in safe_name
    assert len(safe_name) <= 48


def test_user_storage_helpers(tmp_path: Path):
    user_dir = get_user_storage_dir(tmp_path, 123)
    assert user_dir.exists()
    assert user_dir.name == '123'

    file_path = resolve_user_file_path(tmp_path, 123, '../report.txt')
    assert file_path.parent == user_dir.resolve()
    assert file_path.name == 'report.txt'
