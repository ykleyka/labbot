"""Экспорт/импорт очередей в Google Таблицы через сервисный аккаунт.
gspread синхронный, поэтому методы вызываются через asyncio.to_thread."""
import json
import os
import re

import gspread

HEADER = ["№", "ФИО", "Подгруппа", "Уважительная причина"]


class SheetsError(Exception):
    pass


def sheet_title(title: str) -> str:
    """Google не разрешает в названии листа символы []:*?/\\ и длину больше 100."""
    return re.sub(r"[\[\]:*?/\\]", " ", title).strip()[:100]


class SheetsClient:
    def __init__(self, credentials_path: str):
        self.credentials_path = credentials_path
        self._gc: gspread.Client | None = None

    @property
    def available(self) -> bool:
        return os.path.exists(self.credentials_path)

    def service_email(self) -> str | None:
        try:
            with open(self.credentials_path, encoding="utf-8") as f:
                return json.load(f).get("client_email")
        except (OSError, ValueError):
            return None

    def _open(self, url: str):
        if not self.available:
            raise SheetsError(f"Не найден ключ сервисного аккаунта: {self.credentials_path}")
        try:
            if self._gc is None:
                self._gc = gspread.service_account(filename=self.credentials_path)
            return self._gc.open_by_url(url)
        except gspread.exceptions.SpreadsheetNotFound:
            raise SheetsError("Таблица не найдена. Проверьте ссылку и доступ сервисного аккаунта (редактор).")
        except gspread.exceptions.NoValidUrlKeyFound:
            raise SheetsError("Неверная ссылка на таблицу.")
        except gspread.exceptions.GSpreadException as e:
            raise SheetsError(f"Ошибка Google API: {e}")

    def export(self, url: str, tables: dict[str, list[list]]) -> None:
        """tables: название листа -> строки (с заголовком)."""
        sh = self._open(url)
        try:
            existing = {ws.title: ws for ws in sh.worksheets()}
            for title, rows in tables.items():
                size = max(len(rows) + 10, 50)
                ws = existing.get(title)
                if ws is None:
                    ws = sh.add_worksheet(title=title, rows=size, cols=len(HEADER))
                else:
                    ws.clear()
                    ws.resize(rows=size, cols=max(ws.col_count, len(HEADER)))
                ws.update(range_name="A1", values=rows)
        except gspread.exceptions.GSpreadException as e:
            raise SheetsError(f"Ошибка Google API: {e}")

    def read_all(self, url: str) -> dict[str, list[list[str]]]:
        sh = self._open(url)
        try:
            return {ws.title: ws.get_all_values() for ws in sh.worksheets()}
        except gspread.exceptions.GSpreadException as e:
            raise SheetsError(f"Ошибка Google API: {e}")


def parse_sheet_rows(rows: list[list[str]]) -> list[tuple[str, str]]:
    """Строки листа -> [(ФИО, причина)]. Понимает и наш формат (№ | ФИО | ...),
    и просто столбец с ФИО."""
    result = []
    for row in rows:
        cells = [str(c).strip() for c in row]
        if not any(cells):
            continue
        if len(cells) >= 2 and cells[1]:
            name, reason = cells[1], (cells[3] if len(cells) >= 4 else "")
        else:
            name, reason = cells[0], ""
        if not name or name.casefold() == "фио":
            continue
        result.append((name, reason))
    return result
