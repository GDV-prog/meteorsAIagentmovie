"""
Веб-интерфейс консультанта Find My Movie на Streamlit (стиль чата с GPT).

Слева — список чатов (у каждого своя память), в центре — активный диалог.
Каждый чат = отдельная agent.Session (своя ConversationBufferMemory + executor),
поэтому разные чаты не путают контекст.

Запуск:
    uv run streamlit run ui_streamlit.py
Откроется в браузере (http://localhost:8501).

Нужны ключи в .env: LLM_API_KEY (обязательно), TAVILY_API_KEY (для web_search).
И собранная база: uv run python indexer.py
"""

from __future__ import annotations

import os
import uuid

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="Find My Movie", page_icon="🎬", layout="wide")

if not os.getenv("LLM_API_KEY"):
    st.error("Не задан LLM_API_KEY в .env — без него голова не отвечает.")
    st.stop()

import agent  # noqa: E402  (после проверки ключа: импорт тянет модели)

NEW_TITLE = "Новый чат"
ASSISTANT_AVATAR = "🎬"
EXAMPLES = [
    "Что-то напряжённое про выживание в космосе",
    "Добрая комедия на вечер",
    "Похожее на «Интерстеллар»",
    "Атмосферный детектив с неожиданным финалом",
]


@st.cache_resource(show_spinner="Прогреваю эмбеддер и базу…")
def _warmup() -> bool:
    """Один раз на процесс: грузим эмбеддер и клиент базы, чтобы первый
    запрос про фильмы не ждал ~10 c загрузки модели. Кэш — на весь рантайм."""
    try:
        from embeddings import COLLECTION, embed_query
        from tools import _client

        embed_query("прогрев")
        _client().count(COLLECTION)
    except Exception as e:  # прогрев не критичен — просто будет первый запрос медленнее
        print(f"Прогрев пропущен: {e}", flush=True)
    return True


@st.cache_data(show_spinner=False)
def _db_count() -> int | None:
    """Сколько фильмов в базе (для статуса в сайдбаре). None — если база недоступна."""
    try:
        from embeddings import COLLECTION
        from tools import _client

        return _client().count(COLLECTION).count
    except Exception:
        return None


def _chat_to_md(chat: dict) -> str:
    """Диалог → Markdown для экспорта (кнопка «Скачать»)."""
    lines = [f"# {chat['title']}\n"]
    for m in chat["history"]:
        who = "🎬 Find My Movie" if m["role"] == "assistant" else "Вы"
        lines.append(f"**{who}:**\n\n{m['content']}\n")
    return "\n".join(lines)


# ------------------------------------------------------------- работа с чатами -
def _make_chat() -> dict:
    """Новый чат: свежая Session (память + executor) и пустая история."""
    return {
        "id": uuid.uuid4().hex[:8],
        "title": NEW_TITLE,
        "history": [],            # [{"role": "user"|"assistant", "content": str}]
        "session": agent.Session(),
    }


def _active() -> dict:
    """Текущий активный чат (гарантированно существует)."""
    cid = st.session_state.active
    return next(c for c in st.session_state.chats if c["id"] == cid)


def _ensure_state() -> None:
    """Инициализация хранилища чатов при первом заходе."""
    if "chats" not in st.session_state:
        chat = _make_chat()
        st.session_state.chats = [chat]
        st.session_state.active = chat["id"]


def _new_chat() -> None:
    chat = _make_chat()
    st.session_state.chats.append(chat)
    st.session_state.active = chat["id"]


def _select(cid: str) -> None:
    st.session_state.active = cid


def _delete(cid: str) -> None:
    """Удалить чат (вместе с его памятью). Последний — заменяем пустым."""
    st.session_state.chats = [c for c in st.session_state.chats if c["id"] != cid]
    if not st.session_state.chats:
        st.session_state.chats = [_make_chat()]
    if st.session_state.active == cid:
        st.session_state.active = st.session_state.chats[-1]["id"]


def _clear_active() -> None:
    """Очистить активный чат: сбросить память Session и историю сообщений."""
    c = _active()
    c["session"].clear()
    c["history"] = []
    c["title"] = NEW_TITLE


def _submit(text: str) -> None:
    """Положить запрос пользователя в активный чат. Ответ сгенерируется
    отдельным проходом (см. ниже), поэтому здесь — только добавляем ход."""
    text = text.strip()
    if not text:
        return
    c = _active()
    if c["title"] == NEW_TITLE:
        c["title"] = text[:30] + ("…" if len(text) > 30 else "")
    c["history"].append({"role": "user", "content": text})


# ------------------------------------------------------------------ интерфейс --
_warmup()
_ensure_state()

with st.sidebar:
    st.markdown("## 🎬 Find My Movie")
    st.button("➕ Новый чат", use_container_width=True, type="primary", on_click=_new_chat)
    st.button("🧹 Очистить чат", use_container_width=True, on_click=_clear_active)
    st.divider()

    # список чатов: активный подсвечен, рядом — кнопка удаления
    for c in reversed(st.session_state.chats):
        is_active = c["id"] == st.session_state.active
        row, dele = st.columns([5, 1])
        row.button(
            c["title"],
            key=f"sel_{c['id']}",
            use_container_width=True,
            type="primary" if is_active else "secondary",
            on_click=_select,
            args=(c["id"],),
        )
        dele.button("🗑", key=f"del_{c['id']}", on_click=_delete, args=(c["id"],))

    st.divider()

    # экспорт активного диалога
    _cur = _active()
    st.download_button(
        "⬇️ Скачать диалог (.md)",
        data=_chat_to_md(_cur),
        file_name=f"{_cur['title']}.md",
        mime="text/markdown",
        use_container_width=True,
        disabled=not _cur["history"],
    )

    with st.expander("❓ Как спрашивать"):
        st.markdown(
            "- **По настроению:** «что-то напряжённое про космос»\n"
            "- **По похожести:** «как „Интерстеллар“»\n"
            "- **По деталям:** сюжет, год, режиссёр, актёры\n"
            "- Можно уточнять прямо в диалоге — чат помнит контекст."
        )

    # статус: база, web-поиск, модель
    _cnt = _db_count()
    _db = f"{_cnt:,} фильмов".replace(",", " ") if _cnt else "недоступна ⚠️"
    _web = "вкл ✅" if os.getenv("TAVILY_API_KEY") else "выкл"
    _model = agent.LLM_MODEL.split("/")[-1]
    st.caption(f"📀 База: {_db}\n\n🌐 Web-поиск: {_web}\n\n🧠 Модель: {_model}")

# --- центр: активный чат ---
chat = _active()

# приветственный экран на пустом чате — примеры-подсказки
if not chat["history"]:
    st.markdown("#### С чего начнём? 🎬")
    st.caption("Опишите настроение или назовите похожий фильм — или выберите пример:")
    cols = st.columns(2)
    for i, ex in enumerate(EXAMPLES):
        if cols[i % 2].button(ex, key=f"ex_{i}", use_container_width=True):
            _submit(ex)
            st.rerun()

# история (единственный проход рендера завершённых сообщений — без двойной отрисовки)
for m in chat["history"]:
    avatar = ASSISTANT_AVATAR if m["role"] == "assistant" else None
    with st.chat_message(m["role"], avatar=avatar):
        st.markdown(m["content"])

# нужен ли ответ на последний ход пользователя
if chat["history"] and chat["history"][-1]["role"] == "user":
    with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
        with st.spinner("Подбираю…"):
            try:
                reply = chat["session"].respond(chat["history"][-1]["content"])
            except Exception as e:  # не валим интерфейс на ошибке модели/сети
                reply = f"⚠️ Ошибка: {e}"
        st.markdown(reply)
    chat["history"].append({"role": "assistant", "content": reply})

if prompt := st.chat_input("Например: что-то напряжённое про выживание в космосе…"):
    _submit(prompt)
    st.rerun()
