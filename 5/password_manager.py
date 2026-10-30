import argparse
import getpass
import hashlib
import secrets
import sqlite3
import string
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "passwords.db"
KEY_PATH = BASE_DIR / ".key"

MIN_MASTER_LENGTH = 8
MIN_PASSWORD_LENGTH = 8
DEFAULT_PASSWORD_LENGTH = 16
MAX_LOGIN_ATTEMPTS = 3

MENU_TITLE = "Менеджер паролей"
MENU_ITEMS = [
    "1) Добавить новый пароль",
    "2) Получить пароль",
    "3) Список всех паролей",
    "4) Удалить пароль",
    "5) Сгенерировать пароль",
    "6) Выход",
]
MENU_WIDTH = 44
MENU_PROMPT = "Выберите действие 1-6: "
CONFIRM_WORDS = ("y", "yes", "д", "да")



def load_or_create_key(path: Path = KEY_PATH) -> bytes:
    if path.exists():
        key = path.read_bytes().strip()
        if not key:
            raise ValueError("файл ключа пуст")
        return key
    key = Fernet.generate_key()
    path.write_bytes(key)
    return key


def encrypt_password(fernet: Fernet, password: str) -> str:
    return fernet.encrypt(password.encode("utf-8")).decode("utf-8")


def decrypt_password(fernet: Fernet, token: str) -> str:
    return fernet.decrypt(token.encode("utf-8")).decode("utf-8")



def connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            master_hash TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            login TEXT NOT NULL,
            password TEXT NOT NULL
        )
        """
    )
    conn.commit()


def hash_master_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def master_password_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM meta WHERE id = 1").fetchone() is not None


def set_master_password(conn: sqlite3.Connection, password: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (id, master_hash) VALUES (1, ?)",
        (hash_master_password(password),),
    )
    conn.commit()


def verify_master_password(conn: sqlite3.Connection, password: str) -> bool:
    row = conn.execute("SELECT master_hash FROM meta WHERE id = 1").fetchone()
    if row is None:
        return False
    return secrets.compare_digest(row[0], hash_master_password(password))


def add_entry(conn: sqlite3.Connection, fernet: Fernet, name: str, login: str, password: str) -> bool:
    try:
        conn.execute(
            "INSERT INTO entries (name, login, password) VALUES (?, ?, ?)",
            (name, login, encrypt_password(fernet, password)),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return False
    return True


def get_entry(conn: sqlite3.Connection, fernet: Fernet, name: str):
    row = conn.execute(
        "SELECT name, login, password FROM entries WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1], decrypt_password(fernet, row[2])


def list_entries(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT name, login FROM entries ORDER BY name COLLATE NOCASE"
    ).fetchall()


def delete_entry(conn: sqlite3.Connection, name: str) -> bool:
    cursor = conn.execute("DELETE FROM entries WHERE name = ?", (name,))
    conn.commit()
    return cursor.rowcount > 0



def generate_password(length: int = DEFAULT_PASSWORD_LENGTH, use_symbols: bool = True) -> str:
    if length < MIN_PASSWORD_LENGTH:
        raise ValueError(f"длина пароля должна быть не меньше {MIN_PASSWORD_LENGTH} символов")
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
    if use_symbols:
        pools.append(string.punctuation)
    alphabet = "".join(pools)
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(char in pool for char in password) for pool in pools):
            return password



def setup_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def ask_password(prompt: str) -> str:
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(prompt)
    return input(prompt)


def authenticate(conn: sqlite3.Connection) -> None:
    if master_password_exists(conn):
        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            password = ask_password("Введите мастер-пароль: ")
            if verify_master_password(conn, password):
                return
            remaining = MAX_LOGIN_ATTEMPTS - attempt
            if remaining:
                print(f"Неверный мастер-пароль. Осталось попыток: {remaining}")
        print("Вход не выполнен: превышено число попыток.")
        sys.exit(1)

    print("Первый запуск: мастер-пароль не задан, создайте его.")
    while True:
        password = ask_password("Придумайте мастер-пароль: ")
        if len(password) < MIN_MASTER_LENGTH:
            print(f"Пароль должен содержать минимум {MIN_MASTER_LENGTH} символов.")
            continue
        if ask_password("Повторите мастер-пароль: ") != password:
            print("Пароли не совпадают, попробуйте ещё раз.")
            continue
        set_master_password(conn, password)
        print("Мастер-пароль сохранён. Используйте его для входа при следующих запусках.")
        return



def prompt_length() -> int:
    raw = input(f"Длина пароля [{DEFAULT_PASSWORD_LENGTH}]: ").strip()
    if not raw:
        return DEFAULT_PASSWORD_LENGTH
    try:
        return int(raw)
    except ValueError:
        print(f"Ошибка: введите число. Использую длину {DEFAULT_PASSWORD_LENGTH}.")
        return DEFAULT_PASSWORD_LENGTH


def confirm(prompt: str) -> bool:
    answer = input(prompt).strip().lower()
    return answer in ("",) + CONFIRM_WORDS


def cmd_add(conn: sqlite3.Connection, fernet: Fernet, args: argparse.Namespace) -> bool:
    name = (args.name or input("Название/откуда: ")).strip()
    login = (args.login or input("Логин: ")).strip()
    if not name or not login:
        print("Ошибка: название и логин не могут быть пустыми.")
        return False

    if args.generate:
        try:
            password = generate_password(args.length)
        except ValueError as exc:
            print(f"Ошибка: {exc}")
            return False
        print(f"Сгенерированный пароль: {password}")
    elif args.password is not None:
        password = args.password
    else:
        password = ask_password("Пароль: ")
        if ask_password("Повторите пароль: ") != password:
            print("Ошибка: пароли не совпадают.")
            return False

    if not password:
        print("Ошибка: пароль не может быть пустым.")
        return False

    if not add_entry(conn, fernet, name, login, password):
        print(f"Ошибка: запись с названием '{name}' уже существует.")
        return False
    print(f"Запись '{name}' добавлена.")
    return True


def cmd_get(conn: sqlite3.Connection, fernet: Fernet, args: argparse.Namespace) -> bool:
    try:
        record = get_entry(conn, fernet, args.name)
    except InvalidToken:
        print("Ошибка: не удалось расшифровать запись (файл .key не подходит к этой базе).")
        return False
    if record is None:
        print(f"Запись '{args.name}' не найдена.")
        return False
    name, login, password = record
    print(f"Название: {name}")
    print(f"Логин:    {login}")
    print(f"Пароль:   {password}")
    return True


def cmd_list(conn: sqlite3.Connection, fernet: Fernet, args: argparse.Namespace) -> bool:
    rows = list_entries(conn)
    if not rows:
        print("Сохранённых записей нет.")
        return True
    width = max(len(name) for name, _ in rows)
    print(f"{'Название':<{width}} | Логин")
    print("-" * (width + 14))
    for name, login in rows:
        print(f"{name:<{width}} | {login}")
    print(f"\nВсего записей: {len(rows)}")
    return True


def cmd_delete(conn: sqlite3.Connection, fernet: Fernet, args: argparse.Namespace) -> bool:
    if not args.yes:
        answer = input(f"Удалить запись '{args.name}'? (y/n): ").strip().lower()
        if answer not in CONFIRM_WORDS:
            print("Удаление отменено.")
            return True
    if delete_entry(conn, args.name):
        print(f"Запись '{args.name}' удалена.")
        return True
    print(f"Запись '{args.name}' не найдена.")
    return False


def cmd_new(conn: sqlite3.Connection, fernet: Fernet, args: argparse.Namespace) -> bool:
    try:
        password = generate_password(args.length, use_symbols=not args.no_symbols)
    except ValueError as exc:
        print(f"Ошибка: {exc}")
        return False
    print(f"Сгенерированный пароль: {password}")
    return True



def print_menu() -> None:
    lines = [
        "+" + "=" * MENU_WIDTH + "+",
        "|" + MENU_TITLE.center(MENU_WIDTH) + "|",
        "+" + "=" * MENU_WIDTH + "+",
    ]
    lines += ["| " + item.ljust(MENU_WIDTH - 2) + " |" for item in MENU_ITEMS]
    lines.append("+" + "-" * MENU_WIDTH + "+")
    print("\n".join(lines), flush=True)


def run_menu_action(conn: sqlite3.Connection, fernet: Fernet, choice: str) -> None:
    if choice == "1":
        args = argparse.Namespace(name=None, login=None, password=None,
                                  generate=False, length=DEFAULT_PASSWORD_LENGTH)
        cmd_add(conn, fernet, args)
    elif choice == "2":
        name = input("Название записи: ").strip()
        if not name:
            print("Ошибка: название не может быть пустым.")
        else:
            cmd_get(conn, fernet, argparse.Namespace(name=name))
    elif choice == "3":
        cmd_list(conn, fernet, argparse.Namespace())
    elif choice == "4":
        name = input("Название записи: ").strip()
        if not name:
            print("Ошибка: название не может быть пустым.")
        else:
            cmd_delete(conn, fernet, argparse.Namespace(name=name, yes=False))
    elif choice == "5":
        length = prompt_length()
        use_symbols = confirm("Использовать специальные символы? (Y/n): ")
        cmd_new(conn, fernet, argparse.Namespace(length=length, no_symbols=not use_symbols))
    else:
        print("Ошибка: введите число от 1 до 6.")


def interactive_menu(conn: sqlite3.Connection, fernet: Fernet) -> None:
    while True:
        print_menu()
        try:
            choice = input(MENU_PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            return
        if choice == "6":
            print("До свидания!")
            return
        try:
            run_menu_action(conn, fernet, choice)
        except (EOFError, KeyboardInterrupt):
            print("\nВозврат в меню.")
        except Exception as exc:
            print(f"Ошибка: {exc}")
        print()



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="password_manager",
        description="Менеджер паролей: SQLite3 + шифрование Fernet. "
                    "Запуск без команды открывает интерактивное меню.",
    )
    subparsers = parser.add_subparsers(dest="command")

    p_add = subparsers.add_parser("add", help="добавить новую запись")
    p_add.add_argument("-n", "--name", help="название/откуда (например, Google)")
    p_add.add_argument("-l", "--login", help="логин")
    p_add.add_argument("-p", "--password", help="пароль (иначе будет запрошен скрытым вводом)")
    p_add.add_argument("-g", "--generate", action="store_true", help="сгенерировать пароль вместо ввода")
    p_add.add_argument("-L", "--length", type=int, default=DEFAULT_PASSWORD_LENGTH,
                       help="длина генерируемого пароля (по умолчанию 16)")
    p_add.set_defaults(func=cmd_add)

    p_get = subparsers.add_parser("get", help="получить пароль по названию")
    p_get.add_argument("name", help="название записи, например Google")
    p_get.set_defaults(func=cmd_get)

    p_list = subparsers.add_parser("list", help="показать названия и логины всех записей")
    p_list.set_defaults(func=cmd_list)

    p_delete = subparsers.add_parser("delete", help="удалить запись по названию")
    p_delete.add_argument("name", help="название записи")
    p_delete.add_argument("-y", "--yes", action="store_true", help="удалить без подтверждения")
    p_delete.set_defaults(func=cmd_delete)

    p_new = subparsers.add_parser("new", help="сгенерировать новый пароль")
    p_new.add_argument("-l", "--length", type=int, default=DEFAULT_PASSWORD_LENGTH,
                       help="длина пароля (по умолчанию 16)")
    p_new.add_argument("--no-symbols", action="store_true", help="не использовать специальные символы")
    p_new.set_defaults(func=cmd_new)

    return parser


def main(argv=None) -> None:
    setup_streams()
    args = build_parser().parse_args(argv)
    conn = connect()
    try:
        init_db(conn)
        try:
            fernet = Fernet(load_or_create_key())
        except (OSError, ValueError) as exc:
            print(f"Ошибка: не удалось получить ключ шифрования ({KEY_PATH}): {exc}")
            sys.exit(1)
        authenticate(conn)
        if args.command is None:
            interactive_menu(conn, fernet)
        elif not args.func(conn, fernet, args):
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        sys.exit(130)
