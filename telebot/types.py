from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class InlineKeyboardButton:
    text: str
    url: str | None = None
    callback_data: str | None = None


class InlineKeyboardMarkup:
    def __init__(self, row_width: int = 3, keyboard: list[list[Any]] | None = None):
        self.row_width = row_width
        self.keyboard = keyboard or []

    def add(self, *buttons: InlineKeyboardButton, row_width: int | None = None) -> InlineKeyboardMarkup:
        width = row_width or self.row_width
        for index in range(0, len(buttons), width):
            self.keyboard.append(list(buttons[index : index + width]))
        return self

    def row(self, *buttons: InlineKeyboardButton) -> InlineKeyboardMarkup:
        self.keyboard.append(list(buttons))
        return self


class Message:
    def __init__(self, source: Any):
        self._source = source
        self.id = getattr(source, "message_id", getattr(source, "id", None))
        self.message_id = self.id
        self.chat = source.chat
        self.from_user = source.from_user
        self.text = getattr(source, "text", None)
        self.caption = getattr(source, "caption", None)
        self.document = getattr(source, "document", None)
        self.photo = getattr(source, "photo", None)


class CallbackQuery:
    def __init__(self, source: Any):
        self._source = source
        self.id = source.id
        self.data = source.data
        self.from_user = source.from_user
        self.message = Message(source.message)
