from __future__ import annotations
import hashlib
import os
import re
import unicodedata
from typing import Any

# The room's own words in each language it speaks: one catalog, a key for each text, the same keys in every
# language, a parameter by its name — never a sentence put together from translated pieces. Names, ids, paths,
# models and commands to run go in as parameters, as they are. A number takes its form by the language's rule
# (one / few / many in Russian). A key missing in one language is a test's failure, not a quiet English line.
LANGS = ("en", "ru")
LANG_NAMES = {"en": "English", "ru": "Русский"}

CATALOG: dict[str, dict[str, Any]] = {
    # The first run: both languages at once, before anything else.
    "lang.choose": {"en": "Выберите язык / Choose language", "ru": "Выберите язык / Choose language"},
    "lang.room": {"en": "Room language: {language}", "ru": "Язык комнаты: {language}"},
    "lang.change.question": {
        "en": "Change this room's language to {language}?",
        "ru": "Язык комнаты: {language}. Применить?",
    },
    "lang.change.apply": {"en": "Apply", "ru": "Применить"},
    "lang.change.cancel": {"en": "Cancel", "ru": "Отмена"},
    "lang.change.also_new": {"en": "Also for new rooms", "ru": "Использовать и для новых комнат"},
    "lang.changed": {
        "en": "The room speaks English now. Agents get it with their next assignment.",
        "ru": "Язык комнаты изменён на русский. Агенты получат указание отвечать по-русски со следующей вводной.",
    },
    "lang.unchanged": {"en": "Language unchanged.", "ru": "Язык не изменён."},
    # Counts.
    "n.agents": {
        "en": {"one": "{n} agent", "other": "{n} agents"},
        "ru": {"one": "{n} агент", "few": "{n} агента", "many": "{n} агентов"},
    },
    # The chat's command menu.
    "cmd.add": {"en": "start a new agent", "ru": "запустить нового агента"},
    "cmd.remove": {"en": "disconnect a participant", "ru": "отключить участника от комнаты"},
    "cmd.mute": {
        "en": "room-wide messages stop; by name still arrive",
        "ru": "общие сообщения не доставляются; по имени — доходят",
    },
    "cmd.unmute": {"en": "deliver to an agent again", "ru": "снова доставлять агенту общие сообщения"},
    "cmd.unlimit": {
        "en": "an agent is back from its usage limit: deliver to it again",
        "ru": "возобновить доставку агенту после окончания лимита",
    },
    "cmd.name": {"en": "rename yourself or an agent", "ru": "переименовать себя или агента"},
    "cmd.view": {"en": "see an agent's screen (read-only)", "ru": "посмотреть экран агента (только чтение)"},
    "cmd.answer": {"en": "respond in an agent terminal over the chat", "ru": "ответить в терминале агента поверх чата"},
    "cmd.open": {"en": "open an agent's window in a new tab", "ru": "открыть окно агента в новой вкладке"},
    "cmd.pause": {"en": "pause the room: agents get no messages", "ru": "приостановить доставку сообщений агентам"},
    "cmd.resume": {"en": "resume the room: waiting messages go out", "ru": "возобновить доставку ожидающих сообщений"},
    "cmd.clear": {"en": "clear the history (moved to an archive)", "ru": "очистить историю комнаты (переносится в архив)"},
    "cmd.help": {"en": "what would you like to do? — tasks and the guide", "ru": "что вы хотите сделать? — задачи и справка"},
    "cmd.mine": {
        "en": "what is for you: unread messages, agents waiting for you",
        "ru": "сообщения и вопросы к вам",
    },
    "cmd.dm": {
        "en": "talk privately with an agent in a panel beside the chat",
        "ru": "личный разговор с агентом в панели рядом с чатом",
    },
    "cmd.restart": {"en": "restart an agent, its conversation continues", "ru": "перезапустить агента, разговор продолжится"},
    "cmd.team": {"en": "the team: see it, or start a ready one", "ru": "команда: посмотреть, изменить или запустить готовую"},
    "cmd.stop": {
        "en": "stop the room: close its windows and agents",
        "ru": "остановить комнату: закрыть её окна и остановить запущенных ею агентов",
    },
    "cmd.exit": {"en": "close this chat panel", "ru": "закрыть этот чат, агенты продолжат работу"},
    "cmd.language": {"en": "the room's language", "ru": "язык комнаты"},
    # The scenarios' picker and preview.
    "team.what": {"en": "What will we do?", "ru": "Что будем делать?"},
    "team.none": {
        "en": "No template — nothing automatic, add agents with /add",
        "ru": "Без шаблона — добавьте агентов через /добавить",
    },
    "team.none.said": {
        "en": "No template: nothing automatic — /add adds an agent, /team picks a ready team later.",
        "ru": "Без шаблона: добавьте агента через /добавить или позже выберите готовую команду через /команда.",
    },
    "team.preview.head": {
        "en": "{title} — {hint}. This is a starting team: change it with /team once it runs.",
        "ru": "{title} — {hint}. Это начальный состав: после запуска его можно изменить через /команда.",
    },
    "team.preview.result": {
        "en": "Result: {output}. Without you: nothing external.",
        "ru": "Итог: {output}. Без вас: ничего внешнего.",
    },
    "team.result.chat": {"en": "in the chat", "ru": "в чате"},
    "team.launch.question": {"en": "Launch this team?", "ru": "Запустить эту команду?"},
    "team.launch": {"en": "Launch the team — {agents}", "ru": "Запустить команду — {agents}"},
    "team.back": {"en": "Back", "ru": "Назад"},
    "team.unavailable": {"en": "Not available: {what}", "ru": "Недоступно: {what}"},
    "team.has_one": {
        "en": "This room has a team already: /team shows it.",
        "ru": "В этой комнате уже есть команда. Посмотреть состав: /команда.",
    },
    "team.starting": {"en": "Starting the {title} team…", "ru": "Запускаю команду «{title}»…"},
    "team.view.head": {
        "en": "Team: {title} · result: {output} · without you: {without}",
        "ru": "Команда: {title} · итог: {output} · без вас: {without}",
    },
    "team.view.none": {"en": "No scenario", "ru": "Без сценария"},
    "team.view.nothing": {"en": "nothing external", "ru": "ничего внешнего"},
    "team.view.empty": {
        "en": "No agents yet: /team picks a ready team, /add adds one agent.",
        "ru": "Агентов пока нет. Выберите готовую команду через /команда или добавьте агента через /добавить.",
    },
    "team.started": {"en": "Team started: {started} of {total}.", "ru": "Команда запущена: {started} из {total}."},
    "team.started.lead": {"en": " Lead — {lead}.", "ru": " Ведущий — {lead}."},
    "team.started.nolead": {
        "en": " No lead started: /add one, or start the team again.",
        "ru": " Ведущий не запустился: добавьте его через /добавить или запустите команду снова.",
    },
    "state.starting": {"en": "Starting", "ru": "Запускается"},
    "state.getting": {"en": "Getting its assignment", "ru": "Получает назначение"},
    "state.acknowledged": {"en": "Assignment acknowledged", "ru": "Назначение подтверждено"},
    "state.replied": {"en": "Replied to its assignment — read it", "ru": "Агент ответил на вводную — прочитайте ответ"},
    # The guide (/help): each line by its English words' key.
    "help.cc3cb7ce9b": {"en": "Choose a recipient", "ru": "Выбрать получателя"},
    "help.f7ec8b2413": {
        "en": "Open the command menu; ↑/↓ to choose, Enter to select",
        "ru": "Открыть меню команд; ↑/↓ — выбор, Enter — выполнить",
    },
    "help.4b0fd81d82": {"en": "Go back or close the menu", "ru": "Назад или закрыть меню"},
    "help.096c43bb67": {
        "en": "Send a message; multiline pastes stay in one message",
        "ru": "Отправить сообщение; вставка из нескольких строк — одним сообщением",
    },
    "help.2c4259714f": {
        "en": "Earlier messages, one at a time; Esc — back to the newest",
        "ru": "Прежние сообщения по одному; Esc — к последним",
    },
    "help.edda33e098": {
        "en": "An agent needs you — /answer opens its terminal",
        "ru": "Агент ждёт вашего действия — /ответить откроет его терминал",
    },
    "help.ad15d1c0f8": {"en": "Rename yourself or an agent", "ru": "Переименовать себя или агента"},
    "help.105f29f801": {"en": "Add an agent · disconnect a participant", "ru": "Добавить агента · отключить участника"},
    "help.ccdbc7ccb5": {
        "en": "See an agent's screen · open its window in a new tab",
        "ru": "Экран агента · его окно в новой вкладке",
    },
    "help.a1ac7cf483": {"en": "Show this guide", "ru": "Показать эту справку"},
    "help.e9137ff5be": {"en": "Choose a recipient; shown by the input", "ru": "Выбрать получателя; он показан у строки ввода"},
    "help.c58fb8dff4": {
        "en": "New line inside a message (or end the line with \\)",
        "ru": "Новая строка внутри сообщения (или \\ в конце строки)",
    },
    "help.86018ecc92": {
        "en": "Clear the whole draft, even a long pasted one",
        "ru": "Стереть весь черновик, даже длинную вставку",
    },
    "help.b33880db0e": {
        "en": "Earlier messages: one at a time, or a page; Esc — back to the newest",
        "ru": "Прежние сообщения: по одному или страницей; Esc — к последним",
    },
    "help.0bc491285d": {
        "en": "Send to someone without changing the chosen recipient",
        "ru": "Написать кому-то, не меняя выбранного получателя",
    },
    "help.d7eee0ba1f": {
        "en": "An agent needs your input — use /answer, then Ctrl+] to return",
        "ru": "Агенту нужен ваш ответ — /ответить, затем Ctrl+] назад",
    },
    "help.c1ffaa1441": {
        "en": "Green: ready · Yellow: working · Orange: muted or limit · Red !: act in its window",
        "ru": "Зелёный: свободен · Жёлтый: работает · Оранжевый: заглушён или лимит · Красный !: нужен ответ в его окне",
    },
    "help.50af86a220": {
        "en": "× muted · ◷ limit reached · ? no recent data · … connecting",
        "ru": "× заглушён · ◷ лимит · ? нет свежих данных · … подключается",
    },
    "help.85d38c9be9": {
        "en": "Green with a spinner and a number: ready for messages, background tasks still run",
        "ru": "Зелёный с вращающимся значком и числом: принимает сообщения, фоновые задачи ещё выполняются",
    },
    "help.9dfa9c133f": {
        "en": "More agents wrap to another row; none are hidden",
        "ru": "Если агенты не помещаются, их статусы переносятся на следующую строку; никто не скрыт",
    },
    "help.6f3283e0e3": {
        "en": "Start a new agent: choose its kind, its folder, its name",
        "ru": "Запустить нового агента: программа, папка, имя",
    },
    "help.d40ae8935e": {
        "en": "See the team and where each agent is with its assignment; in a new room, start a ready team",
        "ru": "Состав и состояние назначения каждого агента; в новой комнате — запустить готовую команду",
    },
    "help.e8644cfcd3": {
        "en": "Disconnect a participant; the agent keeps running in its window",
        "ru": "Отключить участника; агент продолжает работать в своём окне",
    },
    "help.04405a24f7": {
        "en": "Stop / resume room-wide messages to an agent; messages to it by name still arrive",
        "ru": "Остановить / возобновить общие сообщения агенту; сообщения по имени доходят",
    },
    "help.b474bc9da6": {
        "en": "An agent is back from its usage limit: the room delivers to it again",
        "ru": "Возобновить доставку агенту после окончания лимита",
    },
    "help.abb8b22aa6": {
        "en": "Rename yourself or an agent: choose whom, type the new name",
        "ru": "Переименовать себя или агента: выбрать кого, ввести новое имя",
    },
    "help.33f4ba4960": {
        "en": "See an agent's screen, read-only; Esc — back to chat",
        "ru": "Экран агента, только чтение; Esc — назад в чат",
    },
    "help.344cb72375": {
        "en": "Respond in an agent terminal over the chat; Ctrl+] returns",
        "ru": "Ответить в терминале агента поверх чата; Ctrl+] — назад",
    },
    "help.26a7880074": {
        "en": "Open an agent's window in a new terminal tab, to answer it",
        "ru": "Открыть окно агента в новой вкладке терминала, чтобы ответить ему",
    },
    "help.09e3feee8b": {
        "en": "Restart an agent the room started; its conversation continues (new rules too)",
        "ru": "Перезапустить агента комнаты; разговор продолжится (и с новыми правилами)",
    },
    "help.91954ad89f": {
        "en": "What is for you: agents waiting for you, then unread messages to you and answers to yours",
        "ru": "Агенты, которым нужно ваше действие, затем непрочитанные сообщения и ответы вам",
    },
    "help.8520174043": {
        "en": "Talk with one agent in a panel beside the chat: Ctrl+O there and back, Tab tabs in it, Ctrl+] close",
        "ru": "Разговор с одним агентом в панели рядом с чатом: Ctrl+O туда и обратно, Tab — вкладки, Ctrl+] — закрыть",
    },
    "help.95f5ecec5f": {
        "en": "Pause the room: agents get no messages until /resume",
        "ru": "Пауза: агенты не получают сообщений до /продолжить",
    },
    "help.bf237a3c84": {
        "en": "Clear the history for everyone; old messages move to an archive",
        "ru": "Очистить историю для всех; старые сообщения уходят в архив",
    },
    "help.b1c83d1813": {
        "en": "Stop the room: its windows and the agents it started close; history stays",
        "ru": "Остановить комнату: закрываются её окна и запущенные ею агенты; история остаётся",
    },
    "help.6a4472ea0e": {
        "en": "Close this chat panel (asks first); agents keep running",
        "ru": "Закрыть этот чат после подтверждения; агенты продолжат работу",
    },
    "help.2639bda3b4": {"en": "The room's language: Russian or English", "ru": "Язык комнаты: русский или английский"},
    "help.679993f49d": {"en": "Red status", "ru": "Красный статус"},
    "help.b55b1117dd": {"en": "Status colours", "ru": "Цвета статуса"},
    "help.6400bc3301": {"en": "Status marks", "ru": "Значки статуса"},
    "help.667bf0636e": {"en": "Status layout", "ru": "Строка статусов"},
    "help.c9337cd13e": {"en": "@name text", "ru": "@имя текст"},
    "help.5642921e1d": {"en": "Add an agent", "ru": "Добавить агента"},
    "help.5f1009f1ff": {
        "en": "Choose an agent type, project folder, and name.",
        "ru": "Выберите программу, папку проекта и имя.",
    },
    "help.5eef887242": {
        "en": "The room checks the connection before showing the agent as connected.",
        "ru": "Комната проверит связь и только потом покажет агента подключённым.",
    },
    "help.fe44aa3b1e": {"en": "See what an agent is doing", "ru": "Посмотреть, что делает агент"},
    "help.723ae40430": {"en": "Choose an agent to see its live screen.", "ru": "Выберите агента, чтобы увидеть его экран."},
    "help.213df32fcd": {"en": "This is read-only. Press Esc to return to the chat.", "ru": "Только чтение. Esc — назад в чат."},
    "help.0097af42ee": {"en": "View an agent", "ru": "Посмотреть агента"},
    "help.aa1e92b5c5": {"en": "Open an agent's window in a new tab", "ru": "Открыть окно агента в новой вкладке"},
    "help.b579907bf4": {
        "en": "Choose an agent: its command goes into your clipboard.",
        "ru": "Выберите агента: команда попадёт в буфер обмена.",
    },
    "help.b0dae6b200": {
        "en": "Open a new terminal tab (+), paste with Cmd+V and press Enter.",
        "ru": "Откройте новую вкладку терминала (+), вставьте Cmd+V и нажмите Enter.",
    },
    "help.f7207047c3": {"en": "Open an agent terminal", "ru": "Открыть терминал агента"},
    "help.bb39d21b7d": {"en": "Answer an agent over the chat", "ru": "Ответить агенту поверх чата"},
    "help.b859a4c7a8": {
        "en": "Choose an agent to open its terminal over this chat.",
        "ru": "Выберите агента, чтобы открыть его терминал поверх чата.",
    },
    "help.6e893f15df": {
        "en": "Respond in that window. Press Ctrl+] to return to the chat.",
        "ru": "Ответьте в этом окне. Ctrl+] — назад в чат.",
    },
    "help.ee11988fb4": {"en": "Answer an agent", "ru": "Ответить агенту"},
    "help.69d1258962": {"en": "Pause or resume the room", "ru": "Приостановить или возобновить доставку сообщений"},
    "help.c55cb683e8": {
        "en": "Pause holds delivery to agents and their outgoing messages.",
        "ru": "Пауза задерживает доставку агентам и их исходящие сообщения.",
    },
    "help.4a4dbc7ca9": {
        "en": "A turn already running may finish. Resume releases waiting messages.",
        "ru": "Уже начатая работа может завершиться. После возобновления доставки ожидающие сообщения будут отправлены.",
    },
    "help.e48c7298c4": {"en": "Clear conversation history", "ru": "Очистить историю разговора"},
    "help.d343ad2c96": {
        "en": "Messages move to the room's local archive. Confirm before clearing.",
        "ru": "Сообщения уходят в местный архив комнаты. Перед очисткой — подтверждение.",
    },
    "help.c1783d24d8": {
        "en": "This does not erase an agent's own session history.",
        "ru": "Собственную историю сессии агента это не стирает.",
    },
    "help.53b5158bc7": {"en": "Clear history", "ru": "Очистить историю"},
    "help.15c3fa417b": {"en": "Stop the room", "ru": "Остановить комнату"},
    "help.40373b140a": {
        "en": "Stop asks which room windows will close and preserves history.",
        "ru": "Перед остановкой вы увидите, какие окна закроются. История сохранится.",
    },
    "help.a468e04ed2": {"en": "Stop room", "ru": "Остановить комнату"},
    "help.b823bb9b42": {"en": "All keys", "ru": "Все клавиши"},
    "help.f620876eb7": {
        "en": "The full guide: recipients, multiline input, clearing a draft, history, status indicators,",
        "ru": "Полная справка: получатели, многострочный ввод, стирание черновика, история, значки статуса",
    },
    "help.35c9b64cc8": {"en": "and all commands.", "ru": "и все команды."},
    "help.931e90b732": {"en": "Room guide · {me}", "ru": "Справка комнаты · {me}"},
    "help.1254a7fd3e": {"en": "Pause the room", "ru": "Приостановить доставку"},
    "help.60dd26ce4d": {"en": "Resume the room", "ru": "Возобновить доставку"},
    "help.7b52603bb2": {"en": "What would you like to do?", "ru": "Что вы хотите сделать?"},
    "help.eb72290348": {
        "en": "You can open the guide any time: type /help.",
        "ru": "Справку можно открыть в любой момент: /помощь.",
    },
    "help.88dfd48ffe": {"en": "/dm name \u00b7 /dm close", "ru": "/личное имя · /личное закрыть"},
    # /open: the command in the clipboard, or not.
    "open.copied": {
        "en": "The command is already in your clipboard — you don't need to copy anything.\n  1. Open a new terminal tab (+).\n  2. Paste with Cmd+V and press Enter: {label}'s window opens there.\n  3. Close that tab when done: {label} keeps running, the room stays here.\nIf pasting doesn't work, copy this command yourself:\n\n{line}  {command}\n",
        "ru": "Команда уже в буфере обмена — копировать ничего не нужно.\n  1. Откройте новую вкладку терминала (+).\n  2. Вставьте Cmd+V и нажмите Enter: там откроется окно агента {label}.\n  3. Закончив, закройте вкладку: {label} продолжит работу, комната останется здесь.\nЕсли вставка не сработала, скопируйте команду сами:\n\n{line}  {command}\n",
    },
    "open.copy_failed": {
        "en": "Could not copy automatically — copy this command yourself:\n\n{line}  {command}\n\n  1. Open a new terminal tab (+).\n  2. Paste the command and press Enter: {label}'s window opens there.\n  3. Close that tab when done: {label} keeps running, the room stays here.\n",
        "ru": "Скопировать автоматически не удалось — скопируйте команду вручную:\n\n{line}  {command}\n\n  1. Откройте новую вкладку терминала (+).\n  2. Вставьте команду и нажмите Enter: там откроется окно агента {label}.\n  3. Закончив, закройте вкладку: {label} продолжит работу, комната останется здесь.\n",
    },
    # The team before its launch: changed in the preview.
    "team.edit": {"en": "Change an agent…", "ru": "Изменить агента…"},
    "team.add": {"en": "Add an agent…", "ru": "Добавить агента…"},
    "team.remove": {"en": "Remove an agent…", "ru": "Убрать агента…"},
    "team.which": {"en": "Which agent?", "ru": "Какого агента?"},
    "team.field": {"en": "Change {label}: which?", "ru": "Изменить {label}: что?"},
    "team.field.role": {"en": "Role", "ru": "Роль"},
    "team.field.topic": {"en": "Topic", "ru": "Тема"},
    "team.field.model": {"en": "Model", "ru": "Модель"},
    "team.field.memory": {"en": "Memory", "ru": "Память"},
    "team.type": {"en": "Type it (Esc to go back):", "ru": "Введите (Esc — назад):"},
    "team.memory.compact": {"en": "Compact past 150k", "ru": "Сжимать после 150 тыс."},
    "team.memory.clear": {"en": "Clear after handing in work", "ru": "Очищать после сдачи работы"},
    "team.memory.keep": {"en": "Keep — leave it to the program", "ru": "Не трогать — оставить программе"},
    "team.model.other": {"en": "Another model — type its name…", "ru": "Другая модель — ввести название…"},
    "team.add.role": {"en": "Its role?", "ru": "Какая роль?"},
    "team.add.topic": {"en": "Its topic (Esc to go back):", "ru": "Его тема (Esc — назад):"},
    "team.lead.stays": {
        "en": "The lead stays: the team has one. Change who leads after the launch, in /team.",
        "ru": "Ведущий остаётся: он в команде один. Сменить ведущего можно после запуска, в /команда.",
    },
    "team.bad": {"en": "! {why}. Try again.", "ru": "! {why}. Попробуйте ещё раз."},
    # The chat's steps (m_i18n.tr: by the English template's key).
    "t.f37e48b027": {"en": "  {i}. {x}", "ru": "  {i}. {x}"},
    "t.366e97da4f": {"en": "  · Open agent terminal", "ru": "  · Открыть терминал агента"},
    "t.11f2ca3711": {"en": " (the scenario’s)", "ru": " (из сценария)"},
    "t.331f56bc70": {"en": " · +{others} for everyone", "ru": " · +{others} для всех"},
    "t.b74b3bf63e": {"en": " — not in tmux", "ru": " — не в tmux"},
    "t.e21325923c": {"en": "! Could not restart: {exc}", "ru": "! Не удалось перезапустить: {exc}"},
    "t.7a3229fa1e": {
        "en": "! Could not start the agent: {exc}. Try /add again.",
        "ru": "! Не удалось запустить агента: {exc}. Попробуйте /добавить ещё раз.",
    },
    "t.218676bc00": {"en": "! Left as it is: {why}.", "ru": "! Оставлено как есть: {why}."},
    "t.477dda4fb4": {"en": "! Not applied: {p0}.", "ru": "! Не применено: {p0}."},
    "t.61d9cab430": {"en": "! Not saved: {exc}.", "ru": "! Не сохранено: {exc}."},
    "t.abc1f8a141": {"en": "! Not undone: {p0}.", "ru": "! Не отменено: {p0}."},
    "t.932920b2ee": {
        "en": "! The room has one lead: change who leads in /team → The room → Lead.",
        "ru": "! Ведущий в комнате один: сменить его можно в /команда → Комната → Ведущий.",
    },
    "t.16b5406249": {"en": "! {exc}", "ru": "! {exc}"},
    "t.e6cba430ea": {
        "en": "! {problem}. Try another name, or Enter to keep yours.",
        "ru": "! {problem}. Попробуйте другое имя или нажмите Enter, чтобы оставить своё.",
    },
    "t.2f7d5d5a96": {"en": "! {problem}. Try another name.", "ru": "! {problem}. Попробуйте другое имя."},
    "t.a320506cf5": {"en": "! {why}. Try again.", "ru": "! {why}. Попробуйте ещё раз."},
    "t.62172ab00b": {
        "en": "A model takes effect when the program starts: /restart {p0} — its conversation goes on.",
        "ru": "Модель начинает действовать при запуске программы: /перезапустить {p0} — разговор продолжится.",
    },
    "t.c52ad2e5f1": {"en": "Add an agent. ", "ru": "Добавить агента. "},
    "t.997b30ba7d": {
        "en": "All marked as read. Only what comes from now on counts.",
        "ru": "Всё отмечено прочитанным. Учитывается только новое.",
    },
    "t.4769133026": {"en": "Another model — type its name…", "ru": "Другая модель — ввести название…"},
    "t.b8edb52676": {"en": "Answer whom?", "ru": "Кому ответить?"},
    "t.93737e6326": {"en": "Back in the room.", "ru": "Вы снова в комнате."},
    "t.357e05f79c": {"en": "Browse folders…", "ru": "Выбрать папку…"},
    "t.77dfd2135f": {"en": "Cancel", "ru": "Отмена"},
    "t.a341efcdde": {"en": "Canceled.", "ru": "Отменено."},
    "t.5142b45d6c": {
        "en": "Cannot open {label}'s window over the chat: {why}. Use /open for a command to paste into a new terminal tab.",
        "ru": "Не удалось открыть окно агента {label} поверх чата: {why}. /открыть даст команду для новой вкладки терминала.",
    },
    "t.741cc90aac": {
        "en": "Cannot open {label}'s window: this agent is not in tmux.",
        "ru": "Не удалось открыть окно агента {label}: он не в tmux.",
    },
    "t.bc54138e3e": {"en": "Cannot open {p0}/ — choose another folder", "ru": "Не удаётся открыть {p0}/ — выберите другую папку"},
    "t.8b517863a4": {"en": "Cannot open: this agent is not in tmux.", "ru": "Не удаётся открыть: этот агент не в tmux."},
    "t.43cf571b4e": {"en": "Change the team?", "ru": "Изменить команду?"},
    "t.f8ebf53207": {"en": "Change {name}: which?", "ru": "Агент: {name}. Что изменить?"},
    "t.fe94c5a799": {"en": "Change {p0}…", "ru": "Изменить настройки: {p0}…"},
    "t.3582bef289": {"en": "Changes — look back, undo…", "ru": "Изменения — история и отмена…"},
    "t.9e7dea63b5": {"en": "Choose a folder from the list.", "ru": "Выберите папку из списка."},
    "t.c7cdf2d44b": {"en": "Choose an agent to disconnect", "ru": "Выберите агента для отключения"},
    "t.bd4f5e1b97": {"en": "Choose someone from the list.", "ru": "Выберите из списка."},
    "t.94bdb45292": {"en": "Clear after handing in work", "ru": "Очищать после сдачи работы"},
    "t.9e1ea2547d": {"en": "Clear the room's history?", "ru": "Очистить историю комнаты?"},
    "t.56af79108a": {
        "en": "Clear the room's history? Everyone's feed starts empty; the messages move to an archive in .collab/archive/, they are not deleted.",
        "ru": "Очистить историю комнаты? Лента у всех начнётся с чистого листа; сообщения уйдут в архив .collab/archive/ и не удалятся.",
    },
    "t.cbd7c28a8d": {"en": "Close chat? Agents keep running", "ru": "Закрыть чат? Агенты продолжат работу"},
    "t.a755ebe5b3": {"en": "Compact past 150k", "ru": "Сжимать после 150 тыс. токенов"},
    "t.c8fee26f6e": {"en": "Declined.", "ru": "Отклонено."},
    "t.494393cb0e": {"en": "Disconnect {p0}?", "ru": "Отключить от комнаты? Агент: {p0}."},
    "t.2069dfa40f": {
        "en": "Disconnect {p0}? Room messages will stop. The agent will keep running.",
        "ru": "Отключить от комнаты? Агент: {p0}. Доставка сообщений комнаты этому агенту прекратится. Сам агент продолжит работу.",
    },
    "t.3c98872e12": {
        "en": "Enter a folder path, or press Esc to choose from the list.",
        "ru": "Введите путь к папке или нажмите Esc, чтобы выбрать из списка.",
    },
    "t.86c810d9c4": {"en": "Enter a name.", "ru": "Введите имя."},
    "t.efc99c68ea": {"en": "Enter a number from the list.", "ru": "Введите номер из списка."},
    "t.e862bf2c77": {"en": "Enter path manually…", "ru": "Ввести путь вручную…"},
    "t.c7bafbc97b": {"en": "Every 15 minutes", "ru": "Каждые 15 минут"},
    "t.6f754e7673": {"en": "Every 30 minutes", "ru": "Каждые 30 минут"},
    "t.6a76cd5e94": {"en": "Every hour", "ru": "Каждый час"},
    "t.202a7651ce": {"en": "Folder not found: {p0}", "ru": "Папка не найдена: {p0}"},
    "t.83e8ee572f": {"en": "For you · {new} new", "ru": "Для вас · новых: {new}"},
    "t.8c3bdbb8da": {
        "en": "From its next free minute; what it remembers now is not cut by this.",
        "ru": "Начнёт действовать, когда агент освободится. Текущая история разговора не сокращается.",
    },
    "t.9fe00acebc": {"en": "Goal", "ru": "Цель"},
    "t.25b30d8f5b": {"en": "History cleared. The old messages are in {p0}.", "ru": "История очищена. Старые сообщения — в {p0}."},
    "t.c93eb71b5a": {
        "en": "It gets its new assignment after its current task.",
        "ru": "Новую вводную агент получит после текущей задачи.",
    },
    "t.6572754c51": {"en": "It is no longer in the room.", "ru": "Этого агента уже нет в комнате."},
    "t.dde21bc92a": {"en": "It waits: /team shows it.", "ru": "Предложение ожидает решения. Посмотреть: /команда."},
    "t.8350ea90c6": {"en": "Keep — leave it to the program", "ru": "Не трогать автоматически — оставить программе агента"},
    "t.c6f71d85ae": {"en": "Lead", "ru": "Ведущий"},
    "t.a0a0e97339": {"en": "Lead's rounds", "ru": "Проверки ведущего"},
    "t.f0dcffc2b4": {"en": "Live view unavailable: this agent is not in tmux.", "ru": "Экран недоступен: этот агент не в tmux."},
    "t.89c8a2851d": {"en": "Memory", "ru": "Память"},
    "t.68c2cc7f0c": {"en": "Model", "ru": "Модель"},
    "t.a27079be56": {
        "en": "Mute whom? The room stops sending them messages",
        "ru": "Кого заглушить? Комната перестанет отправлять ему общие сообщения",
    },
    "t.5105519ddf": {
        "en": "Name this agent — Enter takes the suggestion, or type another (Esc to go back):",
        "ru": "Введите имя агента или нажмите Enter, чтобы принять предложенное (Esc — назад):",
    },
    "t.578c643d47": {
        "en": "New name for {p0} (Enter to save, Esc to go back):",
        "ru": "Агент: {p0}. Введите новое имя (Enter — сохранить, Esc — назад):",
    },
    "t.816c52fd2b": {"en": "No", "ru": "Нет"},
    "t.1f363e895e": {"en": "No Claude Code or Codex installation found.", "ru": "Claude Code и Codex не найдены."},
    "t.2d7a2dd5fd": {
        "en": "No agent the room started. Add one with /add.",
        "ru": "Нет агентов, запущенных комнатой. Добавьте агента через /добавить.",
    },
    "t.065f39b57b": {
        "en": "No agents in the room yet. Add one with /add.",
        "ru": "В комнате пока нет агентов. Добавьте агента через /добавить.",
    },
    "t.22a7c99e99": {"en": "No changes yet.", "ru": "Изменений пока нет."},
    "t.4e18f4c6c1": {"en": "No messages for you yet.", "ru": "Сообщений для вас пока нет."},
    "t.c68ad12dd8": {"en": "No one is muted.", "ru": "Никто не заглушён."},
    "t.434038465f": {
        "en": "No one is out of its limit.",
        "ru": "Пока нет агентов, для которых можно возобновить доставку после лимита.",
    },
    "t.ad863f99be": {"en": "No one to disconnect.", "ru": "Некого отключать."},
    "t.dd8fac0ae9": {"en": "No one to mute.", "ru": "Некого заглушить."},
    "t.5beff0b811": {
        "en": "No unread messages. Earlier messages are below.",
        "ru": "Непрочитанных сообщений нет. Ранее полученные сообщения — ниже.",
    },
    "t.6eef664840": {"en": "None", "ru": "Нет"},
    "t.e3de5ab0ca": {"en": "Off", "ru": "Выключен"},
    "t.cf9b77061f": {"en": "Open", "ru": "Открыть"},
    "t.f2a62de137": {"en": "Open an agent's window", "ru": "Открыть окно агента"},
    "t.c1e01db47d": {"en": "Open conversation", "ru": "Открыть разговор"},
    "t.1254a7fd3e": {"en": "Pause the room", "ru": "Приостановить доставку"},
    "t.ea4c7141f1": {"en": "Project folder", "ru": "Папка проекта"},
    "t.a53ced3336": {"en": "Project folder.", "ru": "Папка проекта."},
    "t.c6850787d1": {"en": "Proposal: {p0}", "ru": "Предложение: {p0}"},
    "t.5d83b770c0": {"en": "Rename whom?", "ru": "Кого переименовать?"},
    "t.655fe3daa8": {"en": "Reports to", "ru": "Кому отвечает"},
    "t.825467676e": {"en": "Restart whom? The conversation continues", "ru": "Кого перезапустить? Разговор продолжится"},
    "t.c1c6e4bb38": {"en": "Restart {p0}?", "ru": "Перезапустить агента? Агент: {p0}."},
    "t.32ef885ec8": {
        "en": "Restart {p0}? Its program closes and starts again in the same window with the same conversation; what it was doing at this moment stops. It reads the room's newest rules.",
        "ru": "Перезапустить агента? Агент: {p0}. Программа закроется и запустится снова в том же окне с тем же разговором. Текущая работа прервётся. Агент прочитает новые правила комнаты.",
    },
    "t.da42196b94": {
        "en": "Restarting {p0} {how}. Waiting for a connection check reply.",
        "ru": "Перезапуск агента: {p0} {how}. Ожидается ответ на проверку связи.",
    },
    "t.db2f07e2ee": {"en": "Result file", "ru": "Итоговый файл"},
    "t.60dd26ce4d": {"en": "Resume the room", "ru": "Возобновить доставку"},
    "t.c3f104d136": {"en": "Role", "ru": "Роль"},
    "t.efc007a393": {"en": "Save", "ru": "Сохранить"},
    "t.b71b35584b": {"en": "Save it?", "ru": "Сохранить?"},
    "t.0562a4611f": {"en": "Saved: {p0}.", "ru": "Сохранено: {p0}."},
    "t.9c4157a9ea": {"en": "Settings: {p0}.", "ru": "Настройки: {p0}."},
    "t.e4c4032f92": {
        "en": "Starting {label} in room window {pid}. Waiting for a connection check reply.",
        "ru": "Запуск агента: {label}. Окно комнаты: {pid}. Ожидается ответ на проверку связи.",
    },
    "t.7a36eea3a6": {"en": "Stop the room?", "ru": "Остановить комнату?"},
    "t.2b4e2a5e93": {"en": "Stop the room? ", "ru": "Остановить комнату? "},
    "t.5d71e2b59e": {"en": "Stopping the room…", "ru": "Останавливаю комнату…"},
    "t.e0158b080e": {"en": "Talk privately with whom?", "ru": "С кем поговорить лично?"},
    "t.9e56ed9fa0": {
        "en": "The agents get it with their next assignment.",
        "ru": "Агенты получат новые настройки со следующей вводной.",
    },
    "t.03ad78edb5": {"en": "The default — the scenario's, or the room's", "ru": "По умолчанию — из сценария или комнаты"},
    "t.307bf5daaf": {"en": "The person", "ru": "Пользователь"},
    "t.bd5595b9fb": {"en": "The private-talk panel is closed.", "ru": "Панель личного разговора закрыта."},
    "t.d3bf173bdd": {"en": "The proposal waits: /team shows it.", "ru": "Предложение ожидает решения. Посмотреть: /команда."},
    "t.b50e7cdfdf": {
        "en": "The room: lead, goal, result file, what is done without you…",
        "ru": "Комната: ведущий, цель, итоговый файл, что можно без вас…",
    },
    "t.7e13bd176f": {"en": "Topic", "ru": "Тема"},
    "t.bbbd1b8be2": {"en": "Type it…", "ru": "Ввести…"},
    "t.80a3d87006": {"en": "Type the folder path (Esc to go back):", "ru": "Введите путь к папке (Esc — назад):"},
    "t.39fc721248": {"en": "Undo", "ru": "Отменить"},
    "t.4043c1c52b": {"en": "Undo a change?", "ru": "Отменить изменение?"},
    "t.c75b5d766f": {"en": "Undo it?", "ru": "Отменить?"},
    "t.ff217fd850": {
        "en": "Undo: {p0}. Work done and money spent stay; history cleared from an agent does not come back.",
        "ru": "Отменить: {p0}. Уже выполненная работа и расходы останутся. Историю агента, удалённую при очистке, вернуть нельзя.",
    },
    "t.b75a905e98": {"en": "Undone: {p0}.", "ru": "Отменено: {p0}."},
    "t.06b3f52292": {"en": "Unmute whom?", "ru": "Кого разглушить?"},
    "t.0097af42ee": {"en": "View an agent", "ru": "Посмотреть экран агента"},
    "t.3504a30c97": {"en": "Which agent to start?", "ru": "Какого агента запустить?"},
    "t.26156b3f92": {
        "en": "Whose limit is over? The room sends its messages again",
        "ru": "У какого агента закончился лимит? Доставка этому агенту возобновится",
    },
    "t.fbaaecf658": {"en": "Without you", "ru": "Без вас"},
    "t.5397e0583f": {"en": "Yes", "ru": "Да"},
    "t.e85c149301": {"en": "Yes, a new conversation", "ru": "Да, с новым разговором"},
    "t.ca42c5e892": {"en": "Yes, clear the history", "ru": "Да, очистить историю"},
    "t.65279c0c47": {"en": "Yes, disconnect", "ru": "Да, отключить"},
    "t.1c20aeae21": {"en": "Yes, restart", "ru": "Да, перезапустить"},
    "t.cf2035103b": {"en": "Yes, stop the room", "ru": "Да, остановить комнату"},
    "t.eb72290348": {
        "en": "You can open the guide any time: type /help.",
        "ru": "Справку можно открыть в любой момент: /помощь.",
    },
    "t.8de11b712d": {
        "en": "You stay {p0}. /name changes it any time.",
        "ru": "Ваше имя: {p0}. Изменить его можно в любой момент: /имя.",
    },
    "t.71f9790ac9": {"en": "Your proposal is declined.", "ru": "Ваше предложение отклонено."},
    "t.a5b4993a1b": {"en": "nothing external", "ru": "ничего внешнего"},
    "t.bb1d936d0a": {"en": "that is already someone else's name here", "ru": "это имя здесь уже занято"},
    "t.47a42f9a1d": {"en": "the room", "ru": "комната"},
    "t.36dbc31a85": {"en": "with a new conversation", "ru": "с новым разговором"},
    "t.3b7a6e6ed5": {"en": "with its conversation", "ru": "с прежним разговором"},
    "t.4ea7dd28bd": {"en": "{base} {i}", "ru": "{base} {i}"},
    "t.7ca6266688": {"en": "{label} disconnected from the room.", "ru": "Агент отключён от комнаты: {label}."},
    "t.8b844add31": {"en": "{name} — {p1}: {p2} → {p3}. ", "ru": "{name} — {p1}: {p2} → {p3}. "},
    "t.c674ea1271": {"en": "{p0} (Esc to go back):", "ru": "{p0} (Esc — назад):"},
    "t.467254825d": {"en": "{p0} {p1} — {first}  · {where}", "ru": "{p0} {p1} — {first}  · {where}"},
    "t.b34ea0f452": {"en": "{p0} {words}", "ru": "{p0} {words}"},
    "t.9482534b8f": {"en": "{p0} · {p1}", "ru": "{p0} · {p1}"},
    "t.c9e5038fdf": {"en": "{p0} — {p1}", "ru": "{p0} — {p1}"},
    "t.80a06f1964": {
        "en": "{p0}'s conversation was not found. Start a new one?",
        "ru": "Разговор агента {p0} не найден. Начать новый?",
    },
    "t.f9c643fbe5": {
        "en": "{p0}'s conversation was not found. Start it with a new conversation? What it knew stays only in its files.",
        "ru": "Разговор агента {p0} не найден. Начать новый разговор? Из прежнего разговора сохранятся только сведения, записанные в файлы.",
    },
    "t.4ef4cb9f3b": {"en": "{r} — {words}", "ru": "{r} — {words}"},
    "t.7c934bf993": {"en": "{title}: {p1}{p2}", "ru": "{title}: {p1}{p2}"},
    "t.eb26e0cff7": {"en": "{verb} {line}", "ru": "{verb} {line}"},
    "t.54b4518f67": {"en": "‼ {p0} waits for you: {p1}", "ru": "‼ {p0} ждёт вас: {p1}"},
    "t.0c862003e4": {"en": "── Earlier for you ──", "ru": "── Ранее полученные сообщения для вас ──"},
    "t.36a5bb2cb3": {"en": "✓ Mark all as read", "ru": "✓ Отметить всё прочитанным"},
    # The chat's feed, input, panels.
    "t.1d03be0e85": {
        "en": "\u0001\u001b[1;{p0}m\u0002{me} ›\u0001\u001b[0m\u0002 ",
        "ru": "\u0001\u001b[1;{p0}m\u0002{me} ›\u0001\u001b[0m\u0002 ",
    },
    "t.94b3387112": {"en": "\n  id: {p0}", "ru": "\n  id: {p0}"},
    "t.cffc90e461": {
        "en": "  Live view unavailable: the agent's window is gone",
        "ru": "  Экран недоступен: окна агента больше нет",
    },
    "t.e3c0ca826a": {"en": "  ↑↓ choose · Enter select · Esc back", "ru": "  ↑↓ выбор · Enter — выполнить · Esc — назад"},
    "t.1e567aa3a3": {
        "en": "  ↑↓ choose · Enter select · keep typing to filter",
        "ru": "  ↑↓ выбор · Enter — выполнить · вводите текст для поиска",
    },
    "t.4792f52574": {"en": "  ↳ to {whose} of {p1}: «{first}»{forged}", "ru": "  ↳ на {whose} от {p1}: «{first}»{forged}"},
    "t.1b0893965d": {"en": " Live view · {label} · Read-only ", "ru": " Экран · {label} · только чтение "},
    "t.99de41b1fa": {
        "en": " To send text that starts with /, begin it with //.",
        "ru": " Чтобы отправить текст, начинающийся с /, начните его с //.",
    },
    "t.77a8757457": {"en": " {mark} {cell}{p2}", "ru": " {mark} {cell}{p2}"},
    "t.d13895006a": {"en": " · {n} waiting", "ru": " · ждут: {n}"},
    "t.a86927f3ba": {"en": " · {unseen} new", "ru": " · новых: {unseen}"},
    "t.4fce27695b": {"en": " — did you mean {p0}?", "ru": " — может быть, {p0}?"},
    "t.856e28594d": {"en": " ↑/↓ Earlier/later messages · Esc Latest ", "ru": " ↑/↓ прежние/следующие · Esc — к последним "},
    "t.991d71bed2": {"en": " ⚠ not from the chat", "ru": " ⚠ не из чата"},
    "t.8ab05aa15a": {"en": "! /name <ID> <Name>; IDs: {p0}", "ru": "! /имя <ID> <Имя>; ID: {p0}"},
    "t.bf80bf78aa": {
        "en": "! Choose a recipient with Tab first — your text is kept.",
        "ru": "! Сначала выберите получателя клавишей Tab — текст сохранён.",
    },
    "t.4391e72284": {"en": "! Choose a recipient with Tab first.", "ru": "! Сначала выберите получателя клавишей Tab."},
    "t.ea2d69a293": {"en": "! Could not open the panel: {p0}", "ru": "! Не удалось открыть панель: {p0}"},
    "t.5f7e46d4c6": {"en": "! Could not read the agents' states: {exc}", "ru": "! Не удалось прочитать состояния агентов: {exc}"},
    "t.1bf688e168": {"en": "! Could not show a proposal: {exc}", "ru": "! Не удалось показать предложение: {exc}"},
    "t.88b55a32b7": {"en": "! Could not show message {p0}: {exc}", "ru": "! Не удалось показать сообщение {p0}: {exc}"},
    "t.f0a1cb753f": {"en": "! Couldn't do that: {p0}", "ru": "! Не получилось: {p0}"},
    "t.a35dc506f3": {"en": "! No participant {who}", "ru": "! Участник не найден: {who}"},
    "t.187a342d37": {
        "en": "! The recipient left the room — press Tab to choose who gets your message.",
        "ru": "! Получатель покинул комнату — нажмите Tab и выберите, кому отправить.",
    },
    "t.9e31ba8f74": {
        "en": "! The room's settings can't be read: {p0} — the chat goes on with the participants it had; fix .collab.json and it reads them again.",
        "ru": "! Не удаётся прочитать настройки комнаты: {p0} — чат работает с прежним составом; исправьте .collab.json, и он перечитает их.",
    },
    "t.3a9beef875": {
        "en": "! The window is too narrow for a panel beside the chat — use /answer, or write here.",
        "ru": "! Окно слишком узкое для панели рядом с чатом — используйте /ответить или пишите здесь.",
    },
    "t.d536ae5d28": {"en": "! Unknown command {word}", "ru": "! Неизвестная команда {word}"},
    "t.36742c2a01": {"en": "! empty message", "ru": "! пустое сообщение"},
    "t.33067dd140": {"en": "! {p0}", "ru": "! {p0}"},
    "t.6478c7d6d7": {"en": "! {verb} <name>; names: {p1}", "ru": "! {verb} <имя>; имена: {p1}"},
    "t.5c3dc2850c": {"en": "Beginning of the room · ", "ru": "Начало истории комнаты ·  "},
    "t.68194cd452": {"en": "Folder: {p0}", "ru": "Папка: {p0}"},
    "t.b9e78ced88": {"en": "Muted", "ru": "Заглушён"},
    "t.4c976bc303": {
        "en": "No private-talk panel is open — /dm <name> opens one.",
        "ru": "Панель личного разговора не открыта — её открывает /личное <имя>.",
    },
    "t.1a214eecb7": {"en": "No update", "ru": "Нет обновлений"},
    "t.c7dfb6f1d9": {"en": "Paused", "ru": "Пауза"},
    "t.53da34f938": {
        "en": "Room paused — agents get no messages, and what they say waits until /resume. A started turn may still finish.",
        "ru": "Доставка приостановлена: агенты не получают сообщений, их ответы ожидают отправки. /продолжить возобновит доставку. Уже начатая работа может завершиться.",
    },
    "t.32af39c9fc": {
        "en": "Room resumed — waiting messages go out now.",
        "ru": "Доставка возобновлена — ожидающие сообщения уходят.",
    },
    "t.849f3d9b83": {"en": "Show me around", "ru": "Показать, как всё устроено"},
    "t.6fc09607ae": {"en": "Skip for now", "ru": "Пропустить"},
    "t.2844b8f22b": {
        "en": "The private-talk panel opens only beside the person's own chat window.",
        "ru": "Панель личного разговора открывается только рядом с вашим окном чата.",
    },
    "t.72589deaf1": {
        "en": "The room calls you {mine} — your account's name. Your name (Enter keeps {mine_}):",
        "ru": "Предлагаемое имя из вашей учётной записи: {mine}. Введите своё имя или нажмите Enter, чтобы оставить {mine_}:",
    },
    "t.fad5fe74f2": {"en": "Welcome to your room", "ru": "Добро пожаловать в комнату"},
    "t.950e26eb7e": {
        "en": "Welcome to your room\n  Write to your agents here.\n  Tab chooses who receives your message. Enter sends it.\n  Type / to choose an action. You don't need to remember commands.\n  Each agent works in a window of its own — you never need to go there:\n  /view shows its screen, /answer opens it to reply, Ctrl+] brings you back.",
        "ru": "Добро пожаловать в комнату\n  Пишите агентам здесь.\n  Tab выбирает получателя. Enter отправляет.\n  Наберите /, чтобы выбрать действие. Запоминать команды не нужно.\n  У каждого агента своё окно — заходить туда не нужно:\n  /экран показывает его экран, /ответить открывает его для ответа, Ctrl+] возвращает назад.",
    },
    "t.f8c937aa1a": {"en": "[{p0} → {p1}] {p2}", "ru": "[{p0} → {p1}] {p2}"},
    "t.16fc98ff57": {"en": "choose with Tab", "ru": "выберите клавишей Tab"},
    "t.21894bda14": {"en": "closed: /exit, Ctrl+C or end of input", "ru": "закрыт: /выход, Ctrl+C или конец ввода"},
    "t.3cd336a3a2": {
        "en": "collab-room as {as_}. Write `@id text` to address, plain text goes to all. Ctrl-D to leave.",
        "ru": "collab-room как {as_}. `@id текст` — адресно, просто текст — всем. Ctrl-D — выйти.",
    },
    "t.aed67fda7c": {"en": "exit code {code}", "ru": "код выхода {code}"},
    "t.1f6802066e": {"en": "signal {name}", "ru": "сигнал {name}"},
    "t.6054c593c4": {
        "en": "the /dm panel needs a terminal: open it with /dm in the chat",
        "ru": "панели /личное нужен терминал: откройте её командой /личное в чате",
    },
    "t.f2a8775708": {"en": "your message", "ru": "ваше сообщение"},
    "t.641a680f1b": {"en": "{cmd} {pid}", "ru": "{cmd} {pid}"},
    "t.0e6c911265": {"en": "{label} is not out of its limit.", "ru": "Лимит ещё не закончился. Агент: {label}."},
    "t.fb42014785": {"en": "{label} {p1}.", "ru": "{label} {p1}."},
    "t.890106e942": {
        "en": "{label}'s usage limit cleared — messages go to it again.",
        "ru": "Доставка после лимита возобновлена. Агент: {label}.",
    },
    "t.ac4a9a78f4": {"en": "{old} is now called {p1}.", "ru": "{old} теперь называется {p1}."},
    "t.8f595b21d2": {"en": "{p0} {as_} pid {p2}: {reason}\n", "ru": "{p0} {as_} pid {p2}: {reason}\n"},
    "t.0edfd61c6e": {"en": "{p0} → {to} · {when}  {text}", "ru": "{p0} → {to} · {when}  {text}"},
    "t.a5241959c3": {"en": "{p0}'s message", "ru": "сообщение, автор: {p0}"},
    "t.6c4a390406": {"en": "{who} {p1}", "ru": "{who} {p1}"},
    "t.cf3071e0ac": {"en": "{who} {p1} {target} {p3} ", "ru": "{who} {p1} {target} {p3} "},
    "t.d2c6957cb3": {"en": "· Ctrl+O → DM ", "ru": "· Ctrl+O → личное "},
    "t.e416f88c42": {"en": "· {me} ⇄ {p1} in /dm", "ru": "· {me} ⇄ {p1} в /личное"},
    "t.62eee0ec75": {"en": "· {me} ⇄ {p1} in /dm · {new} new", "ru": "· {me} ⇄ {p1} в /личное · новых: {new}"},
    "t.537f4e5b47": {
        "en": "· {who} runs an older collab — /restart {who_} when convenient.",
        "ru": "· Агент: {who}. Работает старая версия collab. Когда будет удобно: /перезапустить {who_}.",
    },
    "t.fcd678b0d4": {
        "en": "… {rest} more lines — collab show {p1} --room {p2}",
        "ru": "… ещё строк: {rest} — collab show {p1} --room {p2}",
    },
    "t.5c21e7c4cd": {"en": "── Esc — Back to chat ", "ru": "── Esc — назад в чат "},
    "t.086185bbc0": {"en": "── {chars} chars · Ctrl+U Clear draft ", "ru": "── знаков: {chars} · Ctrl+U — стереть черновик "},
    "t.2420f6559b": {
        "en": "── {first}↑/↓ Earlier/later messages · Esc Latest{new} ",
        "ru": "── {first}↑/↓ прежние/следующие · Esc — к последним{new} ",
    },
    "t.02a2ecfa0a": {"en": "── ↑ History · / Commands · Tab Recipient ", "ru": "── ↑ История · / Команды · Tab Получатель "},
    "t.e66583d11c": {
        "en": "── ↑/↓ Previous / next · Enter or Esc — back to the list ",
        "ru": "── ↑/↓ Предыдущее / следующее · Enter или Esc — к списку ",
    },
    "t.72927ad520": {"en": "── ● {mine} for you · /mine ", "ru": "── ● для вас: {mine} · /мне "},
    "t.79df47c718": {
        "en": "━ ↑ History · Ctrl+O chat · Tab tabs · Ctrl+] close · /dm name ",
        "ru": "━ ↑ История · Ctrl+O чат · Tab вкладки · Ctrl+] закрыть · /личное имя ",
    },
    "t.e508fa3f8c": {"en": "┄ Ctrl+O here · Tab tabs ", "ru": "┄ Ctrl+O сюда · Tab вкладки "},
    "t.eccfb31e72": {"en": "⚠ not from the chat", "ru": "⚠ не из чата"},
    # The status bar's words (their English is the key of their colours and marks).
    "status.Connecting": {"en": "Connecting", "ru": "Подключается"},
    "status.Working": {"en": "Working", "ru": "Работает"},
    "status.Ready": {"en": "Ready", "ru": "Свободен"},
    "status.Needs your input": {"en": "Needs your input", "ru": "Ждёт вашего действия"},
    "status.Unsent draft": {"en": "Unsent draft", "ru": "Неотправленный черновик"},
    "status.Unknown": {"en": "Unknown", "ru": "Неизвестно"},
    "status.Disconnected": {"en": "Disconnected", "ru": "Отключён"},
    "status.Out of limit": {"en": "Out of limit", "ru": "Лимит исчерпан"},
    "status.Scrolled \u2014 q to exit": {"en": "Scrolled \u2014 q to exit", "ru": "Просмотр истории — q для выхода"},
    "status.Muted": {"en": "Muted", "ru": "Заглушён"},
    "status.No update": {"en": "No update", "ru": "Нет обновлений"},
    # The room's own lines to people (m_log.room_line), retold in the chat's language.
    "t.848cfd1922": {
        "en": "Agents wrote {n} messages to each other in a row without you, each waking an agent with its whole context. The room is paused: what they say waits. /resume lets them go on.",
        "ru": "Сообщений подряд между агентами без вашего участия: {n}. Каждое требует нового ответа агента и обработки всей истории разговора. Доставка приостановлена, сообщения ожидают отправки. /продолжить возобновит доставку.",
    },
    "t.447bea4401": {
        "en": "By your request, {who} changed: {what}. /team → Changes can undo it.",
        "ru": "По вашей просьбе изменены настройки: {what}. Автор изменения: {who}. Отменить можно в /команда → Изменения.",
    },
    "t.d0fd14358f": {"en": "Setting ignored — {why}.", "ru": "Настройка не применена: {why}."},
    "t.c45545893e": {"en": "Settings changed: {what}.", "ru": "Настройки изменены: {what}."},
    "t.d92a278ea7": {
        "en": "{label} ({base}) was added by collab add, in {folder}.",
        "ru": "Добавлен агент: {label} ({base}). Команда: collab add. Папка: {folder}.",
    },
    "t.963b26645f": {
        "en": "{label}'s usage limit was cleared by hand.",
        "ru": "Доставка после лимита возобновлена вручную. Агент: {label}.",
    },
    "t.810fc1560d": {"en": "{name} connected.", "ru": "Агент подключён: {name}."},
    "t.f8efdc6c87": {"en": "{name} is back from its usage limit.", "ru": "Лимит закончился. Агент: {name}."},
    "t.b7f319b247": {
        "en": "{name} is in the room's settings but not running: the room was running already, and start only returned to it — collab stop, then collab start starts it (history stays).",
        "ru": "{name} есть в настройках комнаты, но не запущен: комната уже работала, и start только вернулся в неё — collab stop, затем collab start запустит его (история сохранится).",
    },
    "t.eb3e073994": {
        "en": "{name}: context {k}k tokens, past {at}k — given /{do}.",
        "ru": "{name}: контекст {k} тыс. токенов, больше {at} тыс. — дана команда /{do}.",
    },
    "t.9e783a5c74": {
        "en": "{name}: handed in its work — given /clear.",
        "ru": "Агент: {name}. Работа сдана, отправлена команда /clear.",
    },
    "t.1112fe46e4": {
        "en": "{name}: its window {target} — {why}. It gets no messages until its window is there again; /remove {name} takes it out of the room.",
        "ru": "{name}: окно {target} — {why}. Сообщения ему не идут, пока окна нет; /отключить {name} уберёт его из комнаты.",
    },
    "t.493abf77d6": {
        "en": "{who} is idle, but hasn't replied in the room ({n} messages). Check the agent's window.",
        "ru": "Агент: {who}. Сейчас свободен, но ответа в комнате нет (сообщений: {n}). Проверьте окно агента.",
    },
    "t.acf1f42ebd": {
        "en": "{who} is idle, but hasn't replied in the room. Check the agent's window.",
        "ru": "Агент: {who}. Сейчас свободен, но ответа в комнате нет. Проверьте окно агента.",
    },
    "t.d73ae69d44": {
        "en": "{who} is out of its usage limit until about {hm}. Messages to it wait and go out again then ({n} to resend).",
        "ru": "Агент: {who}. Лимит исчерпан примерно до {hm}. После этого доставка возобновится. Сообщений для повторной отправки: {n}.",
    },
    "t.f21b5c28df": {
        "en": "{who} received a message but never started on it ({n} messages). Check the agent's window.",
        "ru": "Агент: {who}. Сообщения получены, но работа по ним не началась (сообщений: {n}). Проверьте окно агента.",
    },
    "t.91f5af5da0": {
        "en": "{who} received a message but never started on it. Check the agent's window.",
        "ru": "Агент: {who}. Сообщение получено, но работа по нему не началась. Проверьте окно агента.",
    },
    "t.c37656d73e": {
        "en": "⚠ Settings changed outside the chat: {what}. /team → Changes can undo it.",
        "ru": "⚠ Настройки изменены не из чата: {what}. Отменить можно в /команда → Изменения.",
    },
    # A proposal's card.
    "t.0581a15f01": {"en": "A command line", "ru": "Командная строка"},
    "t.6b55028609": {"en": " (your request: «{request}»)", "ru": " (ваша просьба: «{request}»)"},
    "t.8623e1d9ff": {"en": "{who} proposes{asked} — apply?", "ru": "{who} предлагает{asked} — применить?"},
    "t.cfea419c3b": {"en": "Apply", "ru": "Применить"},
    "t.b59cf9ed55": {"en": "Decline", "ru": "Отклонить"},
    "t.8300071cde": {"en": "Later — /team shows it", "ru": "Отложить — посмотреть в /команда"},
    "label.all": {"en": "Everyone", "ru": "Все"},
    "label.room": {"en": "Room", "ru": "Комната"},
    # The command line, the installer, the room's errors.
    "t.4a7bb2ccd3": {
        "en": '  ! {p0} is not on PATH: use the full path for now; to type just `collab`, add this line to {profile} and open a new terminal: export PATH="{shown}:$PATH"',
        "ru": '  ! {p0} нет в PATH: пока используйте полный путь; чтобы набирать просто `collab`, добавьте эту строку в {profile} и откройте новый терминал: export PATH="{shown}:$PATH"',
    },
    "t.c7213e0f45": {
        "en": "  ! {p0} removed: it ran a copy a Codex may change — fix the above and run install again",
        "ru": "  ! Удалено: {p0}. Эта команда запускала копию, которую может изменять Codex. Исправьте указанные выше проблемы и снова запустите install",
    },
    "t.9e49b1e7da": {
        "en": "  Codex agents a room starts can answer in that room without asking each time: only\n  collab say / ack / inbox, for that room and that agent's own name, through a read-only\n  copy of collab that Codex cannot change. Your project's permissions stay as they are.",
        "ru": "  Агенты Codex, запущенные комнатой, смогут отвечать в своей комнате, не спрашивая каждый раз: только\n  collab say / ack / inbox, для этой комнаты и своего имени, через копию collab только для чтения,\n  которую Codex изменить не может. Права вашего проекта остаются прежними.",
    },
    "t.f6a0f993b6": {"en": "  To install Python 3.11+, run:", "ru": "  Чтобы установить Python 3.11+, выполните:"},
    "t.d46a2d99cb": {"en": "  then: collab install", "ru": "  затем: collab install"},
    "t.8ef75921ef": {"en": "  {p0} [{p1}]: ", "ru": "  {p0} [{p1}]: "},
    "t.4f218193f9": {
        "en": "  · Codex room replies: {p0} ({codex_consent})",
        "ru": "  · Ответы Codex в комнате: {p0} ({codex_consent})",
    },
    "t.4426cf54e9": {"en": "  · Message signing key: {owner_key}", "ru": "  · Ключ подписи сообщений: {owner_key}"},
    "t.aeb645badd": {"en": "  · Python {p0}: found, refused — {why}", "ru": "  · Python {p0}: найден, отклонён — {why}"},
    "t.58984179ae": {
        "en": "  · Python {p0}: passed over — it lies in the folder you are in or an agent's, where an agent may write",
        "ru": "  · Python {p0}: пропущен — находится в вашей текущей папке или рабочей папке агента, доступной агенту для записи",
    },
    "t.e0d5b23380": {"en": "  · Python {p0}: {p1} → {python_file}", "ru": "  · Python {p0}: {p1} → {python_file}"},
    "t.247afe4ba8": {"en": "  · collab command: {link} → {p1} — {what}", "ru": "  · Команда collab: {link} → {p1} — {what}"},
    "t.62b01c7a47": {"en": "  · {name}: {p1}", "ru": "  · {name}: {p1}"},
    "t.58977c58c4": {"en": "  – Claude Code not found — plugin skipped", "ru": "  – Claude Code не найден — плагин пропущен"},
    "t.b1dd69848e": {"en": "  – Codex not found — skill skipped", "ru": "  – Codex не найден — навык пропущен"},
    "t.b0d1f7f6b5": {
        "en": "  – Codex room replies: off — Codex asks before answering (to enable: collab install --codex-room-commands)",
        "ru": "  – Ответы Codex без отдельного разрешения: выключены. Codex будет запрашивать разрешение перед ответом (включить: collab install --codex-room-commands)",
    },
    "t.339a56959c": {"en": "  → the rest runs on Python {p0}", "ru": "  → дальнейшие действия выполняются на Python {p0}"},
    "t.2db50b9c21": {
        "en": "  ✓ Codex room replies: on for Codex agents a room starts from now on",
        "ru": "  ✓ Ответы Codex в комнате: включены для агентов Codex, которых комната запустит с этого момента",
    },
    "t.4607014d12": {"en": "  ✓ Message signing key: {p0} ({owner_key})", "ru": "  ✓ Ключ подписи сообщений: {p0} ({owner_key})"},
    "t.5b76453dca": {"en": "  ✓ Python {p0}: {p1} ({p2})", "ru": "  ✓ Python {p0}: {p1} ({p2})"},
    "t.f0840c7130": {"en": "  ✓ collab command: {link} — {what}", "ru": "  ✓ Команда collab: {link} — {what}"},
    "t.31f2da9e06": {
        "en": "  ✗ Python 3.11 or newer, installed outside your home folder — not found. Install one (macOS: {mac};\n    Linux: your system's python3.11 package), then run collab install again.",
        "ru": "  ✗ Python 3.11 или новее, установленный вне вашей домашней папки, не найден. Установите его (macOS: {mac};\n    Linux: пакет python3.11 вашей системы), затем снова запустите collab install.",
    },
    "t.9db0e1b7bf": {
        "en": "  ✗ Python {p0}: recorded, but {parent} may be changed by others — fix its permissions (chmod 700)",
        "ru": "  ✗ Python {p0}: путь сохранён, но папка {parent} доступна другим для изменения. Исправьте права доступа (chmod 700)",
    },
    "t.f05870ded6": {
        "en": "  ✗ collab command: cannot write to {bin_dir} ({strerror}) — pass a folder you can write to with --bin-dir",
        "ru": "  ✗ Команда collab: нельзя записать в {bin_dir} ({strerror}) — укажите папку, куда можно писать, через --bin-dir",
    },
    "t.72465b50e9": {
        "en": "  ✗ collab command: needs Python 3.11 or newer to check where Codex may write — install one, then run collab install again",
        "ru": "  ✗ Команда collab: нужен Python 3.11 или новее, чтобы проверить, куда может писать Codex — установите его и снова запустите collab install",
    },
    "t.35dfcd6ebb": {"en": "  ✗ collab command: {strerror}", "ru": "  ✗ Команда collab: {strerror}"},
    "t.7fb87916d9": {
        "en": "  ✗ {link} already exists and is not ours — left untouched (use another --bin-dir)",
        "ru": "  ✗ Уже существует: {link}. Этот объект не принадлежит collab и оставлен без изменений (укажите другую папку через --bin-dir)",
    },
    "t.4108b9fe05": {
        "en": "  ✗ {p0} did not finish — nothing else was run",
        "ru": "  ✗ Действие не завершено: {p0}. Следующие действия не запускались",
    },
    "t.fb3f5c7540": {
        "en": "--context-trim takes TOKENS:compact, TOKENS:clear or off — say 150000:compact",
        "ru": "--context-trim принимает TOKENS:compact, TOKENS:clear или off — например 150000:compact",
    },
    "t.5104bc08a4": {
        "en": "--context-trim: a {p0} takes only off for now",
        "ru": "--context-trim: {p0} пока принимает только off",
    },
    "t.cfeb71b12e": {"en": "--kind is one of: {p0}", "ru": "Допустимые значения --kind: {p0}"},
    "t.7e4ca894d4": {
        "en": "--no-reply closes named messages only: use --id",
        "ru": "--no-reply закрывает только явно указанные сообщения: используйте --id",
    },
    "t.33df9ec31a": {
        "en": "--quiet is for notes nobody answers; an answer (--re) is never quiet",
        "ru": "--quiet — для уведомлений, не требующих ответа. Для ответа (--re) этот флаг недоступен",
    },
    "t.ce62c8722e": {
        "en": "--session must be 1 to 64 letters, digits, _ and -",
        "ru": "Значение --session: от 1 до 64 символов — буквы, цифры, _ и -",
    },
    "t.18f57ea0fd": {
        "en": "--set takes ID.KEY=VALUE (or room.KEY=VALUE): {item}",
        "ru": "--set принимает ID.KEY=VALUE (или room.KEY=VALUE): {item}",
    },
    "t.ffe32d1c30": {"en": "--{p0}: {problem}", "ru": "--{p0}: {problem}"},
    "t.bbe42eef96": {
        "en": "A deliverer runs outside the room's window (pid {outside}) — stop it (kill {outside_}), then run `collab start` again.",
        "ru": "Доставка запущена вне окна комнаты (pid {outside}) — остановите её (kill {outside_}) и снова запустите `collab start`.",
    },
    "t.9266eabff5": {
        "en": "A deliverer runs outside the room's window — find it (ps ax | grep 'collab deliver'), stop it, then run `collab start` again.",
        "ru": "Доставка запущена вне окна комнаты — найдите её (ps ax | grep 'collab deliver'), остановите и снова запустите `collab start`.",
    },
    "t.3493cc6a78": {
        "en": "A room already exists here. Opening it with its history.",
        "ru": "Здесь уже есть комната. Открываю её вместе с историей.",
    },
    "t.30afd20cf7": {
        "en": "Acknowledged {p0} message(s) for {participant}.",
        "ru": "Сообщения отмечены прочитанными. Участник: {participant}. Количество: {p0}.",
    },
    "t.1153b79222": {"en": "Attach: tmux attach -t {session}", "ru": "Подключиться: tmux attach -t {session}"},
    "t.c38fe76c84": {
        "en": "Cannot open this folder: {p0}. Choose another folder.",
        "ru": "Не удаётся открыть эту папку: {p0}. Выберите другую.",
    },
    "t.02153a63f1": {
        "en": "Clear the room's history? It is moved to an archive, not deleted.",
        "ru": "Очистить историю комнаты? Она уйдёт в архив, а не удалится.",
    },
    "t.9c69e316e5": {
        "en": "Closed {p0} message(s) for {participant} without a reply.",
        "ru": "Сообщения закрыты без ответа. Участник: {participant}. Количество: {p0}.",
    },
    "t.af20c6f853": {
        "en": "Created the room ({who}) in {base}; the project folder is unchanged. Run `collab start`.",
        "ru": "Комната создана ({who}) в {base}; папка проекта не изменилась. Запустите `collab start`.",
    },
    "t.703b98addc": {"en": "Creating a room in {p0}.", "ru": "Создаю комнату в {p0}."},
    "t.610b305584": {"en": "Folder not found: {folder}", "ru": "Папка не найдена: {folder}"},
    "t.56538443ce": {"en": "Folder not found: {typed}", "ru": "Папка не найдена: {typed}"},
    "t.953d2d139e": {
        "en": "Held: the room is paused; your message goes out when it resumes.",
        "ru": "Сообщение ожидает отправки: доставка приостановлена. Оно будет отправлено после возобновления доставки.",
    },
    "t.d427609b52": {"en": "History moved to {p0}.", "ru": "История перенесена в {p0}."},
    "t.887d4eb0fb": {"en": "Keep them and start them?", "ru": "Оставить их и запустить?"},
    "t.5c71417d34": {
        "en": "Let room-started Codex agents answer in their room?",
        "ru": "Разрешить агентам Codex, запущенным комнатой, отвечать в своей комнате?",
    },
    "t.3799c93dac": {
        "en": "Name the participants (Enter keeps the current name).",
        "ru": "Назовите участников (Enter оставляет текущее имя).",
    },
    "t.fe0b2d741d": {"en": "No current tasks.", "ru": "Текущих задач нет."},
    "t.301483df52": {
        "en": "No unacknowledged messages for {participant}.",
        "ru": "Непрочитанных сообщений нет. Участник: {participant}.",
    },
    "t.93044c9aa8": {
        "en": "Paused: agents wrote {streak} messages to each other without the person; wait for the person.",
        "ru": "Доставка приостановлена. Сообщений подряд между агентами без участия пользователя: {streak}. Дождитесь пользователя.",
    },
    "t.4f05197e66": {"en": "Project folder: ", "ru": "Папка проекта: "},
    "t.0d7e041b8a": {
        "en": "Proposed {p0}: {p1}. The person applies it with Enter in the chat.",
        "ru": "Предложение {p0}: {p1}. Пользователь применяет его клавишей Enter в чате.",
    },
    "t.d4f9f0162b": {
        "en": "Queued {p0} from {sender} to {p2}.",
        "ru": "Сообщение {p0} поставлено в очередь. Отправитель: {sender}. Получатели: {p2}.",
    },
    "t.d85e0464ae": {
        "en": "Room created for {p0}. Pick a ready team or add agents in the chat.",
        "ru": "Комната создана. Пользователь: {p0}. Выберите готовую команду или добавьте агентов в чате.",
    },
    "t.313d53b09e": {"en": "Room files moved out of the project: {p0}", "ru": "Файлы комнаты перенесены из проекта: {p0}"},
    "t.bce8e4ced9": {
        "en": "Run `collab deliver` (any terminal) so tmux participants receive their messages.",
        "ru": "Запустите `collab deliver` (в любом терминале), чтобы участники в tmux получали сообщения.",
    },
    "t.86e8c6a19c": {
        "en": "Run `collab` in a terminal to choose, or `collab start` in a project folder.",
        "ru": "Запустите `collab` в терминале, чтобы выбрать комнату, или `collab start` в папке проекта.",
    },
    "t.011a5b7f94": {
        "en": "Running rooms keep the old code: `collab start` in a room moves its delivery; /restart an agent, reopen a chat.",
        "ru": "Работающие комнаты продолжают использовать прежний код. Выполните `collab start` в комнате, чтобы обновить доставку, перезапустите агента через /перезапустить и откройте чат заново.",
    },
    "t.609a8714bd": {
        "en": "Starting {label} in room window {pid}. It joins when {how} (collab status).",
        "ru": "Запуск агента: {label}. Окно комнаты: {pid}. Условие подключения: {how} (collab status).",
    },
    "t.6eba897d3a": {
        "en": "Stop the room? Agents started by the room will be closed.",
        "ru": "Остановить комнату? Запущенные ею агенты прекратят работу.",
    },
    "t.a30a388f14": {
        "en": "The active venv {p0} is left out of the room: its windows run without it.",
        "ru": "Активное виртуальное окружение {p0} не передаётся в комнату: её окна запускаются без него.",
    },
    "t.6a688d339e": {
        "en": "The old {old_state} was not removed: {strerror}",
        "ru": "Не удалось удалить прежние данные: {old_state}. Причина: {strerror}",
    },
    "t.94a6a3f95e": {"en": "The room is not running.", "ru": "Комната не запущена."},
    "t.1f8a3f3206": {"en": "The room is stopped.", "ru": "Комната остановлена."},
    "t.22f8badc0a": {
        "en": "The room's history was among them: the moved room starts without it.",
        "ru": "История комнаты была среди них: перенесённая комната начнётся без неё.",
    },
    "t.930dc206b1": {
        "en": "The tmux session name was another room's ({p0}); this room now uses {session}.",
        "ru": "Имя сессии tmux принадлежало другой комнате ({p0}); эта комната теперь использует {session}.",
    },
    "t.9ff3748e83": {
        "en": "Claude Code not found — its plugin is not updated.",
        "ru": "Claude Code не найден — его плагин не обновлён.",
    },
    "t.f0473f76ec": {
        "en": "Claude Code has no collab-room plugin — `collab install` adds it.",
        "ru": "В Claude Code нет плагина collab-room — его ставит `collab install`.",
    },
    "t.582f1b1a14": {
        "en": "The Claude plugin is not updated: claude plugin {p0} said: {p1}",
        "ru": "Плагин Claude не обновлён: claude plugin {p0} ответил: {p1}",
    },
    "t.cb75161c85": {
        "en": "The Claude plugin is not updated: {p0}",
        "ru": "Плагин Claude не обновлён: {p0}",
    },
    "t.f90ccbb730": {
        "en": "The Claude plugin {p0} is the newest.",
        "ru": "Плагин Claude {p0} — последний.",
    },
    "t.3871a30053": {
        "en": "The Claude plugin updated from {p0} to {p1}: Claude agents take its new hooks after /restart.",
        "ru": "Плагин Claude обновлён с {p0} до {p1}: агенты Claude возьмут новые хуки после /restart.",
    },
    "t.2b442e315c": {
        "en": "✓ Use this folder — {p0}",
        "ru": "✓ Выбрать эту папку — {p0}",
    },
    "t.de7eb62f4d": {
        "en": "↑ Parent folder",
        "ru": "↑ Папка выше",
    },
    "t.fb219b5cbd": {
        "en": "  {p0} — no access",
        "ru": "  {p0} — нет доступа",
    },
    "t.c97ff9e019": {
        "en": "↑/↓ Move · Enter Open or select · Esc Back",
        "ru": "↑/↓ — выбор · Enter — открыть или выбрать · Esc — назад",
    },
    "t.a1d1224904": {
        "en": "THIS FOLDER",
        "ru": "ЭТА ПАПКА",
    },
    "t.9203aabbe5": {
        "en": "New room — {p0}",
        "ru": "Новая комната — {p0}",
    },
    "t.8ff673cbe0": {
        "en": "YOUR ROOMS",
        "ru": "ВАШИ КОМНАТЫ",
    },
    "t.69a793863f": {
        "en": "Open room — {p0}",
        "ru": "Открыть комнату — {p0}",
    },
    "t.be88a5afdf": {
        "en": "CREATE A ROOM",
        "ru": "СОЗДАТЬ КОМНАТУ",
    },
    "t.28828b3251": {
        "en": "Choose another project folder…",
        "ru": "Выбрать другую папку проекта…",
    },
    "t.7b52603bb2": {
        "en": "What would you like to do?",
        "ru": "Что вы хотите сделать?",
    },
    "t.f4b3f4fbee": {
        "en": "Choose an existing room, or create one for a project.",
        "ru": "Выберите комнату или создайте новую для проекта.",
    },
    "t.25566e0923": {
        "en": "↑/↓ Move · Enter Select · Esc Cancel",
        "ru": "↑/↓ — выбор · Enter — выбрать · Esc — отмена",
    },
    "t.b53c0a24a5": {
        "en": "A new room asks only for your name. Add agents later with /add.",
        "ru": "Новая комната спросит только ваше имя. Агентов добавите потом через /добавить.",
    },
    "t.0d0735214d": {
        "en": "↑/↓ choose · Enter · Esc — No",
        "ru": "↑/↓ — выбор · Enter · Esc — нет",
    },
    "t.c5c275d5ec": {
        "en": "No Python 3.11 or newer was found. collab needs one, so that Codex agents can answer in the room without asking each time.",
        "ru": "Python 3.11 или новее не найден. Он нужен collab, чтобы агенты Codex отвечали в комнате, не спрашивая каждый раз.",
    },
    "t.35b9758fdd": {
        "en": "{room} · Room chat",
        "ru": "{room} · Чат комнаты",
    },
    "t.5ae8515b53": {
        "en": "Direct messages · {name}",
        "ru": "Личные сообщения · {name}",
    },
    "t.e7596a0986": {
        "en": "Direct messages",
        "ru": "Личные сообщения",
    },
    "t.ff03d50cc9": {
        "en": "DM · {name}",
        "ru": "ЛС · {name}",
    },
    "t.05a3b9fa04": {
        "en": "DM",
        "ru": "ЛС",
    },
    "t.f2493a3e04": {
        "en": "The update at collab start is off. `collab update` still updates when you run it; --on turns it back on.",
        "ru": "Автоматическое обновление при collab start выключено. Команда `collab update` по-прежнему запускает обновление вручную. Включить автоматическое обновление: collab update --on.",
    },
    "t.9d1cad5f3a": {
        "en": "The update at collab start is on: each start looks for a newer collab and installs it.",
        "ru": "Автоматическое обновление при collab start включено: при каждом запуске проверяется наличие новой версии collab. Если она есть, она устанавливается.",
    },
    "t.b8b6b97060": {
        "en": "This room's file holds start commands besides your chat, or windows to type into:",
        "ru": "В настройках этой комнаты, помимо вашего чата, указаны команды запуска или окна для автоматического ввода сообщений:",
    },
    "t.c9ca29fe5d": {"en": "Watching {p0}; Ctrl-C to stop.", "ru": "Слежу за {p0}; Ctrl-C — остановить."},
    "t.1dfb69e0c9": {"en": "Your name [{p0}]: ", "ru": "Ваше имя [{p0}]: "},
    "t.6d7b4fb46f": {
        "en": "a commit is its hex id, 7 to 40 characters: {commit}",
        "ru": "Укажите идентификатор коммита: от 7 до 40 шестнадцатеричных символов. Получено: {commit}",
    },
    "t.d018414c5a": {
        "en": "a task id is letters, digits, «.», «-» or «_», at most 40, starting with a letter or digit: {task}",
        "ru": "ID задачи: не более 40 символов — буквы, цифры, «.», «-» или «_». Первый символ — буква или цифра. Получено: {task}",
    },
    "t.0b0ef6d81f": {
        "en": "add --yes to clear the history without a question",
        "ru": "добавьте --yes, чтобы очистить историю без подтверждения",
    },
    "t.7a54194352": {
        "en": "add --yes to stop the room without a question",
        "ru": "добавьте --yes, чтобы остановить комнату без подтверждения",
    },
    "t.2c78ea5baf": {
        "en": "an agent {verb} only its own seat ({p1}); {pid} is someone else's — ask the person",
        "ru": "агент {verb} только своё место ({p1}); {pid} — чужое, спросите человека",
    },
    "t.9002bf7fc3": {
        "en": "another deliverer is already running for this room — it delivers, nothing to do; to restart it, run `collab start` (it replaces the room's deliverer)",
        "ru": "для этой комнаты уже работает процесс доставки. Дополнительный процесс не нужен. Чтобы перезапустить доставку, выполните `collab start`",
    },
    "t.e275dae649": {"en": "cannot read {config_path}: {exc}", "ru": "не удаётся прочитать {config_path}: {exc}"},
    "t.b78c52617b": {"en": "cannot read {messages_path}: {strerror}", "ru": "не удаётся прочитать {messages_path}: {strerror}"},
    "t.c8bc620333": {
        "en": "cannot read {panes_path} ({exc}); read as empty",
        "ru": "не удаётся прочитать {panes_path} ({exc}); используется пустой список",
    },
    "t.3fbaaaf1e6": {"en": "cannot write {path}: {strerror}", "ru": "не удаётся записать {path}: {strerror}"},
    "t.42acb6f93e": {"en": "choose either --id <message-id> or --all", "ru": "выберите либо --id <message-id>, либо --all"},
    "t.d8d02d36c2": {"en": "collab team start takes one of: {p0}", "ru": "Допустимые значения для collab team start: {p0}"},
    "t.973689bacc": {"en": "collab: {message}", "ru": "collab: {message}"},
    "t.9cfca10436": {
        "en": "could not tell which Claude session runs this command ({exc}); nothing was changed",
        "ru": "не удалось определить, какая сессия Claude запускает эту команду ({exc}); ничего не изменено",
    },
    "t.34afc96fca": {"en": "could not type {p0} into {p1}: {p2}", "ru": "не удалось набрать {p0} в {p1}: {p2}"},
    "t.960c1e80a6": {"en": "delivery tick failed: {exc}", "ru": "ошибка при проверке доставки: {exc}"},
    "t.d5a6bbdebb": {
        "en": "delivery to {p0} failed: {exc}",
        "ru": "не удалось доставить сообщение. Получатель: {p0}. Причина: {exc}",
    },
    "t.2d731f5d5a": {
        "en": "git is required for report: install it (macOS: xcode-select --install) and run report again",
        "ru": "для report нужен git: установите его (macOS: xcode-select --install) и запустите report снова",
    },
    "t.6bb6b0aae0": {
        "en": "git {p0} did not answer in {GIT_TIMEOUT} s in {p2}; nothing was sent — check that repository and run report again",
        "ru": "git {p0} не ответил за {GIT_TIMEOUT} с в {p2}; ничего не отправлено — проверьте репозиторий и запустите report снова",
    },
    "t.81b6457bff": {"en": "git {p0} failed in {p1}: {p2}", "ru": "ошибка git {p0}. Папка: {p1}. Причина: {p2}"},
    "t.ba17ca309d": {"en": "kind is one of: {p0}", "ru": "Допустимые значения kind: {p0}"},
    "t.db177464fb": {"en": "lane is one of: {p0}", "ru": "Допустимые значения lane: {p0}"},
    "t.cdfe74cf29": {"en": "message cannot be empty", "ru": "сообщение не может быть пустым"},
    "t.d19ffa4a78": {"en": "no message {id}", "ru": "сообщение не найдено: {id}"},
    "t.5d01cb77df": {"en": "no message {re} to answer", "ru": "не найдено сообщение для ответа: {re}"},
    "t.e7ddbd8bb9": {"en": "no message {re} to reply to", "ru": "не найдено сообщение для ответа: {re}"},
    "t.dacd7ccff3": {
        "en": "no tmux participant has a pane; run `collab start` or set `target`",
        "ru": "ни у одного участника в tmux нет окна; запустите `collab start` или задайте `target`",
    },
    "t.7aaaf33b61": {"en": "no {name}; run `collab init` first", "ru": "нет {name}; сначала выполните `collab init`"},
    "t.44fb1d4e3b": {
        "en": "nobody needs a pane here; add a participant without `target` (e.g. the owner's chat)",
        "ru": "здесь никому не нужно окно; добавьте участника без `target` (например, чат владельца)",
    },
    "t.b5b7699de9": {
        "en": "not addressed to {participant}: {p1}",
        "ru": "сообщение адресовано другому участнику. Проверяемый участник: {participant}. Сообщение: {p1}",
    },
    "t.52f1f90cc7": {
        "en": "not moved: .collab.json in the project is not a room's file — check it and run `collab` again",
        "ru": "не перенесено: .collab.json в проекте — не файл комнаты; проверьте его и снова запустите `collab`",
    },
    "t.6a99053c91": {
        "en": "not moved; check .collab.json in the project and run `collab` again",
        "ru": "не перенесено; проверьте .collab.json в проекте и снова запустите `collab`",
    },
    "t.0eca49ead5": {"en": "not proposed: {problem}", "ru": "не предложено: {problem}"},
    "t.0476e7a614": {
        "en": "only the room's lead proposes changes of the team; others ask the lead",
        "ru": "предлагать изменения команды может только ведущий комнаты. Остальные участники обращаются к ведущему",
    },
    "t.de874dfedf": {
        "en": "refusing the lock {p0}: {strerror} (a link there?)",
        "ru": "не удалось получить блокировку {p0}: {strerror} (возможно, обнаружена символическая ссылка)",
    },
    "t.f4e9846758": {
        "en": "refusing the room lock: {strerror} (a link in the room's state?)",
        "ru": "не удалось получить блокировку комнаты: {strerror} (возможно, в данных комнаты есть символическая ссылка)",
    },
    "t.3621375318": {
        "en": "refusing the status lock: {strerror} (a link in the room's state?)",
        "ru": "не удалось получить блокировку статусов: {strerror} (возможно, в данных комнаты есть символическая ссылка)",
    },
    "t.b24b7ecea8": {
        "en": "refusing to move the room: {home} is already there — nothing was changed",
        "ru": "комната не перенесена: {home} уже есть — ничего не изменено",
    },
    "t.f083bc206e": {
        "en": "refusing to move the room: {old_config} or {old_state} is a link",
        "ru": "комната не перенесена: {old_config} или {old_state} — ссылка",
    },
    "t.dd5ce03dfc": {
        "en": "refusing to move the room: {old_state} is a link",
        "ru": "комната не перенесена: {old_state} — ссылка",
    },
    "t.ce9a58e6e7": {"en": "refusing to move the room: {path} is a link", "ru": "комната не перенесена: {path} — ссылка"},
    "t.ed340de6c1": {
        "en": "refusing to move the room: {path} is a link or has a second name",
        "ru": "комната не перенесена: {path} — символическая ссылка или файл с несколькими жёсткими ссылками",
    },
    "t.b79183fe0d": {
        "en": "refusing to write {path}: not a plain file of the room",
        "ru": "запись отклонена: {path} — не обычный файл комнаты",
    },
    "t.cb5ecff673": {
        "en": "refusing to write {path}: {strerror} (a link in the room's state?)",
        "ru": "запись отклонена: {path}. Причина: {strerror} (возможно, в данных комнаты есть символическая ссылка)",
    },
    "t.d1740f727b": {
        "en": "refusing: {messages_path} is a link — the room's state must be its own files",
        "ru": "операция отклонена: {messages_path} — символическая ссылка. Данные комнаты должны храниться в её собственных файлах",
    },
    "t.286f6b9ce7": {
        "en": "refusing: {path} is a link — the room's state must be its own files",
        "ru": "операция отклонена: {path} — символическая ссылка. Данные комнаты должны храниться в её собственных файлах",
    },
    "t.4cf5eb5d8d": {
        "en": "refusing: {path} is a link — the room's state must be its own folder",
        "ru": "операция отклонена: {path} — символическая ссылка. Данные комнаты должны храниться в её собственной папке",
    },
    "t.1c4f2befe5": {
        "en": "run it in a terminal tab: collab open {id}",
        "ru": "запустите во вкладке терминала: collab open {id}",
    },
    "t.fc36210c55": {
        "en": "skipped {p0} malformed message(s) for {p1}",
        "ru": "пропущены сообщения с неверным форматом. Участник: {p1}. Количество: {p0}",
    },
    "t.617c725aaf": {"en": "state must be working, idle or needs_owner", "ru": "состояние — working, idle или needs_owner"},
    "t.b34da0ad12": {
        "en": "task {action} needs the task's id and --as <participant>",
        "ru": "Для task {action} укажите ID задачи и --as <участник>",
    },
    "t.0679a3d4b2": {
        "en": "the project folder's path has control characters: {p0} — rename the folder",
        "ru": "в пути папки проекта есть управляющие символы: {p0} — переименуйте папку",
    },
    "t.040cf057c7": {
        "en": "the room is not running — collab start first, then collab add",
        "ru": "комната не запущена — сначала collab start, затем collab add",
    },
    "t.76994b75dc": {
        "en": "the room is not running — collab start first, then collab team start",
        "ru": "комната не запущена — сначала collab start, затем collab team start",
    },
    "t.c86137b8b7": {
        "en": "the room's settings must be a JSON object — in {p0}; fix it there",
        "ru": "настройки комнаты должны быть объектом JSON. Исправьте файл: {p0}",
    },
    "t.3d69b0b7f6": {
        "en": "this copy of collab only answers in a room: say, ack, inbox",
        "ru": "эта копия collab поддерживает только команды комнаты: say, ack, inbox",
    },
    "t.7ca6862873": {
        "en": "this is the installed copy of collab — update from your clone: git pull, then python3 {where} install",
        "ru": "это установленная копия collab — обновляйте из своего клона: git pull, затем python3 {where} install",
    },
    "t.6f42d6a163": {
        "en": "this room has a team already: collab team shows it",
        "ru": "в этой комнате уже есть команда. Посмотреть состав: collab team",
    },
    "t.282f939794": {
        "en": "tmux cannot open {p0}: macOS keeps this folder private from the program that started tmux (Documents, Desktop and Downloads are protected), so every window of the room would close at once — the «[exited]» you saw. Use a project folder outside them, or allow access in System Settings → Privacy & Security → Files and Folders.",
        "ru": "tmux не может открыть {p0}: macOS закрывает эту папку от программы, запустившей tmux (Документы, Рабочий стол и Загрузки защищены), поэтому все окна комнаты сразу бы закрылись — то «[exited]», что вы видели. Используйте папку проекта вне них или разрешите доступ в Системных настройках → Конфиденциальность и безопасность → Файлы и папки.",
    },
    "t.cc1011de19": {
        "en": "tmux did not answer in {TMUX_TIMEOUT} s to `{p1}`: check that it runs (`tmux ls`), then run the command again",
        "ru": "tmux не ответил за {TMUX_TIMEOUT} с на `{p1}`: проверьте, что он работает (`tmux ls`), и выполните команду снова",
    },
    "t.9b2f8a89c9": {
        "en": "tmux is required: {p0}, and run `collab start` again",
        "ru": "нужен tmux: {p0}, и снова запустите `collab start`",
    },
    "t.817fe7af00": {"en": "tmux {p0} failed: {reason}", "ru": "ошибка tmux {p0}: {reason}"},
    "t.aa9a5f9543": {"en": "unknown participant {pid}; known: {p1}", "ru": "неизвестный участник {pid}; известны: {p1}"},
    "t.57c263510a": {"en": "unknown recipient(s): {p0}", "ru": "неизвестные получатели: {p0}"},
    "t.0cb0256fe3": {"en": "unknown sender {sender}", "ru": "неизвестный отправитель {sender}"},
    "t.d467c8b6fa": {"en": "{as_}: {state}", "ru": "{as_}: {state}"},
    "t.23c01a5aa7": {
        "en": "{bin_dir} is a file, not a folder — pass a folder with --bin-dir",
        "ru": "{bin_dir} — файл, а не папка; укажите папку через --bin-dir",
    },
    "t.c7f0dbd90d": {
        "en": "{differs}: the command line is ambiguous; give each of --room, --from/--for once, plainly",
        "ru": "{differs}: командная строка неоднозначна; задайте --room и --from/--for по одному разу, явно",
    },
    "t.43ea27f9c3": {
        "en": "{id} is a person's seat — rename it with /name in their chat",
        "ru": "{id} — место человека; переименовать его можно через /имя в его чате",
    },
    "t.e300653d67": {"en": "{id}: its window is gone — nothing to open", "ru": "{id}: его окна больше нет — открывать нечего"},
    "t.ed7dcd65bf": {
        "en": "{id}: no window of this agent in a running room (an agent outside tmux cannot be opened)",
        "ru": "{id}: нет окна этого агента в работающей комнате (агента вне tmux открыть нельзя)",
    },
    "t.dde24d8c88": {"en": "{id}: {label}", "ru": "{id}: {label}"},
    "t.c9c734d096": {"en": "{label} was not muted.", "ru": "Заглушение общих сообщений не включено. Агент: {label}."},
    "t.165e1fbede": {"en": "{message} — in {p1}; fix it there", "ru": "{message} — в {p1}; исправьте там"},
    "t.872c2b13dd": {
        "en": "{name} already exists (use --force to replace it)",
        "ru": "{name} уже существует (--force заменит его)",
    },
    "t.084fe3cd36": {"en": "{name}: {p1} — {p2}", "ru": "{name}: {p1} — {p2}"},
    "t.f2e522ec18": {
        "en": "{p0} is not a git repository: give the checkout with --repo <folder>; nothing was sent",
        "ru": "{p0} — не репозиторий git: укажите рабочую копию через --repo <папка>; ничего не отправлено",
    },
    "t.5948ef2674": {"en": "{p0}: could not restart it: {p1}", "ru": "{p0}: не удалось перезапустить: {p1}"},
    "t.89a84f46f7": {"en": "{p0}: could not start it again: {p1}", "ru": "{p0}: не удалось запустить снова: {p1}"},
    "t.95028d8f09": {"en": "{p0}: its conversation was not found", "ru": "{p0}: его разговор не найден"},
    "t.a814dabffb": {
        "en": "{p0}: only an agent the room started can be restarted",
        "ru": "{p0}: перезапустить можно только агента, запущенного комнатой",
    },
    "t.f628c542e6": {"en": "{p0}: {p1} ({p2}{lane}) since {p4}", "ru": "{p0}: {p1} ({p2}{lane}) с {p4}"},
    "t.95a80769e3": {
        "en": "{pid} never took {task}: take it first (collab task take)",
        "ru": "Задача {task} не была взята участником {pid}. Сначала возьмите задачу (collab task take)",
    },
    "t.ccf09a79ed": {"en": "{repeated} is given more than once", "ru": "Значение указано несколько раз: {repeated}"},
    "t.1f40369079": {"en": "{who} is on {task} ({kind}).", "ru": "Участник: {who}. Задача: {task}. Вид работы: {kind}."},
    "t.4c1fb487bc": {
        "en": "«{label}» is already {other}'s name in this room — choose another",
        "ru": "«{label}» — уже имя участника {other} в этой комнате; выберите другое",
    },
    "t.09b2fd3f5b": {
        "en": "«{p0}» ({pid}) is another running Claude session in this room. Speak under your own ID; a new agent is added by the person with /add.",
        "ru": "«{p0}» ({pid}) — другая работающая сессия Claude в этой комнате. Пишите под своим ID; нового агента добавляет пользователь через /добавить.",
    },
    "t.81cd79f9a5": {
        "en": "· Codex room replies: would withdraw consent and remove the rules (dry run — nothing changed)",
        "ru": "· Ответы Codex в комнате: согласие было бы отозвано, правила удалены (пробный запуск — ничего не изменено)",
    },
    "t.7db4d1db31": {
        "en": "✓ Codex room replies: off — consent withdrawn, rules removed. A Codex already running\n  keeps its permission until it restarts.",
        "ru": "✓ Ответы Codex в комнате: выключены — согласие отозвано, правила удалены. Уже работающий Codex\n  сохраняет разрешение до перезапуска.",
    },
    # A change's words, an agent's state line.
    "change.default": {"en": "the default", "ru": "по умолчанию"},
    "change.nothing": {"en": "nothing", "ru": "ничего"},
    "change.off": {"en": "off", "ru": "выключен"},
    "change.room": {"en": "the room", "ru": "комната"},
    "change.field.role": {"en": "role", "ru": "роль"},
    "change.field.topic": {"en": "topic", "ru": "тема"},
    "change.field.use_model": {"en": "model", "ru": "модель"},
    "change.field.context_trim": {"en": "memory", "ru": "память"},
    "change.field.reports_to": {"en": "reports to", "ru": "кому отвечает"},
    "change.field.seat": {"en": "seat", "ru": "место"},
    "change.field.lead": {"en": "lead", "ru": "ведущий"},
    "change.field.goal": {"en": "goal", "ru": "цель"},
    "change.field.round": {"en": "round", "ru": "обход"},
    "change.field.output": {"en": "output", "ru": "итоговый файл"},
    "change.field.without_person": {"en": "without_person", "ru": "без вас"},
    "state.joining": {"en": "is connecting", "ru": "подключается"},
    "state.working": {"en": "is working", "ru": "работает"},
    "state.idle": {"en": "is ready", "ru": "свободен"},
    "state.needs_owner": {"en": "needs your input", "ru": "ждёт вашего действия"},
    "state.draft": {
        "en": "has an unsent draft — send or clear it in its window to resume message delivery",
        "ru": "оставил неотправленный черновик — отправьте или сотрите его в окне агента, чтобы доставка продолжилась",
    },
    "state.unknown": {"en": "status unknown", "ru": "состояние неизвестно"},
    "state.lost": {"en": "disconnected", "ru": "отключён"},
    "state.limited": {"en": "is out of its usage limit", "ru": "исчерпал лимит"},
    "state.scrolled": {
        "en": "is scrolled back — press q in its window to resume message delivery",
        "ru": "прокручен назад — нажмите q в окне агента, чтобы доставка продолжилась",
    },
    "t.7ec69e9de5": {
        "en": "· {pid}: No update for {n} min (last seen: {last})",
        "ru": "· {pid}: нет обновлений {n} мин (последнее состояние: {last})",
    },
    "t.680d190dae": {
        "en": "· {pid}: No update for {n} min, delivery is not running (last seen: {last})",
        "ru": "· {pid}: нет обновлений {n} мин, доставка не работает (последнее состояние: {last})",
    },
    "t.cf406f9e7f": {"en": "· {pid} is ready · 1 background task", "ru": "· {pid} свободен · 1 фоновая задача"},
    "t.6f05436a11": {"en": "· {pid} is ready · {n} background tasks", "ru": "· {pid} свободен · фоновых задач: {n}"},
    "t.07a0dab780": {
        "en": "{mark} {pid} needs your input. Open the agent's window to continue.",
        "ru": "{mark} {pid} ждёт вашего действия. Откройте окно агента, чтобы продолжить.",
    },
    "t.54210e4539": {"en": "{mark} {pid} {words}: {detail}", "ru": "{mark} {pid} {words}: {detail}"},
    "t.0ff603ac65": {"en": "{mark} {pid} {words}", "ru": "{mark} {pid} {words}"},
    "t.b3b8fb8245": {"en": "· {pid}: No status yet", "ru": "· {pid}: состояния пока нет"},
    # The person's own scenarios.
    "t.2a5cedad69": {
        "en": "! A name of letters, digits, - and _, up to 40. Try again.",
        "ru": "! Имя из букв, цифр, - и _, не длиннее 40. Попробуйте ещё раз.",
    },
    "t.e953a8b140": {"en": "! Not saved: {why}.", "ru": "! Не сохранено: {why}."},
    "t.d075396f90": {"en": "! left out: {why}", "ru": "! пропущен: {why}"},
    "t.f3d8933e60": {
        "en": "A name for this team (letters, digits, - and _; Esc to go back):",
        "ru": "Имя для этой команды (буквы, цифры, - и _; Esc — назад):",
    },
    "t.5c495230d2": {"en": "Replace your scenario «{name}»?", "ru": "Заменить ваш сценарий «{name}»?"},
    "t.24f1971b57": {"en": "Save as your own scenario…", "ru": "Сохранить как свой сценарий…"},
    "t.b82767fd9a": {
        "en": "Saved as your scenario «{name}»: a new room offers it in «What will we do?».",
        "ru": "Сохранено как ваш сценарий «{name}»: новая комната предложит его в «Что будем делать?».",
    },
    "t.b1fbc8a5fd": {
        "en": "Saved {path}: collab team start own:{name} in a new room.",
        "ru": "Сохранено {path}: collab team start own:{name} в новой комнате.",
    },
    "t.25ef35d7ad": {
        "en": "Your scenario «{name}»: the roles and settings, not the conversations, folders or history.",
        "ru": "Ваш сценарий «{name}»: роли и настройки, без разговоров, папок и истории.",
    },
    "t.8f6954d27f": {
        "en": "collab team save takes a name of letters, digits, - and _, up to 40",
        "ru": "collab team save принимает имя из букв, цифр, - и _, не длиннее 40",
    },
    "t.a8562383eb": {"en": "not saved: {why}", "ru": "не сохранено: {why}"},
    "t.186eb65301": {"en": "your own team", "ru": "ваша собственная команда"},
    "t.114278384a": {"en": "{name}: {p1} — your own team", "ru": "{name}: {p1} — ваша собственная команда"},
    # The lead's rounds.
    "t.af78f31f86": {
        "en": "- {name} holds {task} for {mins} min and wrote nothing in this round.",
        "ru": "- {name} держит {task} уже {mins} мин и за этот обход ничего не написал.",
    },
    "t.2f7f6b874b": {
        "en": "- {name} is free with no task, and the goal is not reached.",
        "ru": "- {name} свободен без задачи, а цель не достигнута.",
    },
    "t.b143e75070": {
        "en": "- {name} handed in {task} at {at}; no answer from you yet.",
        "ru": "- {name} сдал {task} в {at}; вашего ответа пока нет.",
    },
    "t.dd5b3cef88": {"en": "The team's round (every {n} min):", "ru": "Обход команды (каждые {n} мин):"},
    "t.eefc100803": {
        "en": "Act — nudge, give a task, check, ask the person — or answer «going» in one line. Goal reached: collab goal reached --from {lead}.",
        "ru": "Действуйте — подтолкните, дайте задачу, проверьте, спросите пользователя — или ответьте одной строкой «идёт». Цель достигнута: collab goal reached --from {lead}.",
    },
    "t.5ffccc851b": {
        "en": "only the room's lead declares the goal reached",
        "ru": "объявить цель достигнутой может только ведущий комнаты",
    },
    "t.e872c7f207": {"en": "the task", "ru": "задача"},
    "t.1eb90fe84e": {
        "en": "{lead} declared the goal reached: {goal}. The lead's rounds stop; /team can continue them.",
        "ru": "{lead} объявил цель достигнутой: {goal}. Обходы ведущего остановлены; продолжить их можно в /команда.",
    },
    "t.41ffe8ecac": {"en": "Goal reached: the rounds stop.", "ru": "Цель достигнута: обходы остановлены."},
    "t.ee91f47e87": {"en": "Continue the lead's rounds", "ru": "Продолжить обходы ведущего"},
    "t.1bb0846c57": {"en": "The lead's rounds go on.", "ru": "Обходы ведущего продолжаются."},
    # Scenario titles and hints.
    # A ready team's own topics (store.topic_shown): by the English words the scenario gives.
    "topic.c242f8bba1": {"en": "coordination", "ru": "координация"},
    "topic.9ecc8459ea": {"en": "frontend", "ru": "фронтенд"},
    "topic.754a08ddf8": {"en": "backend", "ru": "бэкенд"},
    "topic.773e94c5f1": {"en": "tests and security", "ru": "тесты и безопасность"},
    "topic.05535ecff7": {"en": "summary", "ru": "итоговая сводка"},
    "topic.331f5ae803": {"en": "prices and terms", "ru": "цены и условия"},
    "topic.cbffc617cd": {"en": "reviews and risks", "ru": "отзывы и риски"},
    "topic.c49ece765b": {"en": "fact checking", "ru": "проверка фактов"},
    "topic.f64cd8e32f": {"en": "author", "ru": "написание текста"},
    "topic.9781e3e48c": {"en": "editing and tone", "ru": "редактура и тон"},
    "topic.d0c18034a8": {"en": "recommendation", "ru": "рекомендация"},
    "topic.c95259de1f": {"en": "money", "ru": "деньги"},
    "topic.ac723defad": {"en": "risks", "ru": "риски"},
    "topic.70ef1c723d": {"en": "customers", "ru": "клиенты"},
    "topic.3b11cf8c6b": {"en": "devil's advocate", "ru": "адвокат дьявола"},
    "topic.7b6486d2b5": {"en": "running the project", "ru": "ведение проекта"},
    "topic.d65297b64d": {"en": "the project's topic", "ru": "тема проекта"},
    "topic.d5a63c95d4": {"en": "everything", "ru": "всё"},
    "scenario.development.title": {"en": "Development", "ru": "Разработка"},
    "scenario.development.hint": {
        "en": "Split the work, check the changes, get code",
        "ru": "Разделить работу, проверить изменения, получить код",
    },
    "scenario.research.title": {"en": "Research", "ru": "Исследование"},
    "scenario.research.hint": {
        "en": "Gather facts, check them, get a conclusion with sources",
        "ru": "Собрать сведения, проверить факты, получить вывод с источниками",
    },
    "scenario.text.title": {"en": "Text", "ru": "Текст"},
    "scenario.text.hint": {"en": "Write a text and have it checked", "ru": "Подготовить текст и проверить его"},
    "scenario.decision.title": {"en": "Decision", "ru": "Решение"},
    "scenario.decision.hint": {
        "en": "Compare the options, see the arguments and where they disagree",
        "ru": "Сравнить варианты и увидеть доводы и разногласия",
    },
    "scenario.long.title": {"en": "Long work", "ru": "Долгая работа"},
    "scenario.long.hint": {
        "en": "Run a project and keep its state between your visits",
        "ru": "Вести проект и сохранять его состояние между посещениями",
    },
    "scenario.assistant.title": {"en": "One assistant", "ru": "Один помощник"},
    "scenario.assistant.hint": {"en": "Work with one agent directly", "ru": "Работать напрямую с одним агентом"},
    # Roles, memory, whom one reports to — as shown.
    "role.lead": {"en": "lead", "ru": "ведущий"},
    "role.worker": {"en": "worker", "ru": "исполнитель"},
    "role.reviewer": {"en": "reviewer", "ru": "проверяющий"},
    "role.none": {"en": "no role", "ru": "без роли"},
    "model.default": {"en": "default model", "ru": "модель по умолчанию"},
    "memory.keep": {"en": "keep", "ru": "не трогать автоматически"},
    "memory.compact": {"en": "compact past {k}k", "ru": "сжимать после {k} тыс."},
    "memory.clear": {"en": "clear past {k}k", "ru": "очищать после {k} тыс."},
    "memory.clear.handoff": {"en": "clear after handing in work", "ru": "очищать после сдачи работы"},
    "reports.to": {"en": "reports to {whom}", "ru": "отвечает: {whom}"},
    "whom.person": {"en": "the person", "ru": "вам"},
    # The assignment an agent gets.
    "brief.head": {"en": "Your assignment in this room", "ru": "Ваше назначение в этой комнате"},
    "brief.version": {"en": " (version {v})", "ru": " (версия {v})"},
    "brief.role": {"en": "- Role: {role} — {what}.", "ru": "- Роль: {role} — {what}."},
    "brief.role.none": {"en": "- Role: none set.", "ru": "- Роль: не задана."},
    "brief.topic": {"en": "- Topic: {topic}.", "ru": "- Тема: {topic}."},
    "brief.report": {"en": "- Report to: {whom}.", "ru": "- Кому отвечаете: {whom}."},
    "brief.lead": {
        "en": "- Lead: {lead} — gathers the result and the questions for the person.",
        "ru": "- Ведущий: {lead} — собирает итог и вопросы к пользователю.",
    },
    "brief.you_lead": {
        "en": "- You are the room's lead: the person hears from you; ask them what only they can decide.",
        "ru": "- Вы — ведущий комнаты: собирайте итог для пользователя и обращайтесь к нему с вопросами, которые может решить только он.",
    },
    "brief.goal": {"en": "- Goal: {goal} You answer for reaching it.", "ru": "- Цель: {goal} Вы отвечаете за её достижение."},
    "brief.team": {
        "en": "- Your team: {team}. Give each its part, have the result checked, gather it — do not do their parts yourself.",
        "ru": "- Ваша команда: {team}. Распределите работу, организуйте проверку и соберите итог. Не выполняйте чужие части работы сами.",
    },
    "brief.result": {
        "en": "- Result file: {output}, in the project folder. Keep the result there, not in messages.",
        "ru": "- Итоговый файл: {output}, в папке проекта. Сохраняйте результат в этом файле, а не в сообщениях.",
    },
    "brief.without.some": {
        "en": "- Without the person you may: {what}. Anything else external — ask first.",
        "ru": "- Без пользователя можно: {what}. Перед другими действиями вне комнаты спросите разрешение.",
    },
    "brief.without.none": {
        "en": "- Without the person: nothing external — ask before publishing, writing outside the room or spending.",
        "ru": "- Без человека: ничего внешнего — спросите, прежде чем публиковать, писать вне комнаты или тратить деньги.",
    },
    "brief.language": {"en": "- Answer in English.", "ru": "- Отвечайте по-русски."},
    "brief.not_task": {
        "en": "This is your assignment, not a task: wait for one from {whom}.",
        "ru": "Это назначение, а не задача. Кто даст задачу: {whom}.",
    },
    "brief.foot": {
        "en": "These are the room's settings; they change no permissions of your tools. After /compact or /clear they come again.",
        "ru": "Это настройки комнаты; права ваших инструментов они не меняют. После /compact или /clear они придут снова.",
    },
    "whom.person.from": {"en": "the person", "ru": "пользователь"},
    "whom.person.to": {"en": "the person", "ru": "человеку"},
    "agent.word": {"en": "agent", "ru": "агент"},
    "role.lead.what": {
        "en": "you split the work, gather the result and the questions for the person",
        "ru": "вы распределяете работу, собираете итог и вопросы к пользователю",
    },
    "role.worker.what": {
        "en": "you do your part of the work and hand it to whom you report to",
        "ru": "вы делаете свою часть работы и сдаёте её тому, кому отвечаете",
    },
    "role.reviewer.what": {
        "en": "you look for mistakes in the result and say what to fix; you do not redo the work",
        "ru": "вы ищете ошибки в результате и говорите, что исправить; работу заново не делаете",
    },
    "without.merge": {"en": "merge into the main branch", "ru": "вливать в основную ветку"},
    "without.publish": {"en": "publish", "ru": "публиковать"},
    "without.send": {"en": "write to anyone outside the room", "ru": "писать кому-либо вне комнаты"},
    "without.spend": {"en": "spend money", "ru": "тратить деньги"},
}

# The chat's commands: each by its English name and its Russian one, both always understood. The first is the
# one the room works with; the second is shown in a Russian room's menu and help.
COMMANDS: list[tuple[str, str]] = [
    ("/add", "/добавить"),
    ("/remove", "/отключить"),
    ("/mute", "/заглушить"),
    ("/unmute", "/разглушить"),
    ("/unlimit", "/снятьлимит"),
    ("/name", "/имя"),
    ("/view", "/экран"),
    ("/answer", "/ответить"),
    ("/open", "/открыть"),
    ("/pause", "/пауза"),
    ("/resume", "/продолжить"),
    ("/clear", "/очистить"),
    ("/help", "/помощь"),
    ("/mine", "/мне"),
    ("/dm", "/личное"),
    ("/restart", "/перезапустить"),
    ("/team", "/команда"),
    ("/stop", "/остановить"),
    ("/exit", "/выход"),
    ("/quit", "/выйти"),
    ("/language", "/язык"),
]
SUBCOMMANDS: dict[str, dict[str, str]] = {"/dm": {"закрыть": "close"}, "/team": {"сохранить": "save"}}
_TO_EN = {unicodedata.normalize("NFC", ru).casefold(): en for en, ru in COMMANDS} | {en: en for en, _ in COMMANDS}


def plural(lang: str, n: int) -> str:
    """The form of a word for a number: one / few / many in Russian (21 агент, 22 агента, 25 агентов, 11–14 —
    агентов), one / other in English."""
    if lang != "ru":
        return "one" if n == 1 else "other"
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def lang_of(value: object) -> str:
    return value if isinstance(value, str) and value in LANGS else "en"


def pseudo(text: str) -> str:
    """With COLLAB_PSEUDO set — a check for whoever makes the screens, never a language of a room — each line of the
    catalog's text a third longer and in ⟦ ⟧: a line the screen cuts loses its ⟧, a line that went past the catalog
    has no ⟦ at all."""
    if not os.environ.get("COLLAB_PSEUDO"):
        return text
    return "\n".join(f"⟦{x}{'·' * (len(x) // 3)}⟧" if x.strip() else x for x in text.split("\n"))


def words_in(key: str, lang: str = "en", **params: Any) -> str:
    """The text for a key in a language, its parameters put in whole. A count named `n` takes its form."""
    entry = CATALOG[key][lang_of(lang)]
    if isinstance(entry, dict):
        entry = entry[plural(lang_of(lang), int(params.get("n", 0)))]
    return pseudo(str(entry).format(**params) if params else str(entry))


def command(line: str) -> str:
    """A line typed in the chat with its command in English, whichever name was typed: the command's token only,
    by Unicode NFC and without case — the rest (names, text) as it is. Its subcommand too («/личное закрыть»).
    Not a command — the line as it came."""
    if not line.startswith("/") or line.startswith("//"):
        return line
    token, sep, rest = line.partition(" ")
    en = _TO_EN.get(unicodedata.normalize("NFC", token).casefold())
    if en is None:
        return line
    if rest:
        sub, sep2, tail = rest.partition(" ")
        sub_en = SUBCOMMANDS.get(en, {}).get(unicodedata.normalize("NFC", sub).casefold())
        if sub_en:
            rest = sub_en + sep2 + tail
    return en + sep + rest


def shown_command(en: str, lang: str) -> str:
    """A command as a room in this language shows it: Russian in a Russian room, English otherwise."""
    return next((ru for e, ru in COMMANDS if e == en), en) if lang_of(lang) == "ru" else en


def help_words(en: str, lang: str, **params: Any) -> str:
    """A line of the guide in the room's language, by its English words (catalog key help.<hash>)."""
    return words_in("help." + hashlib.sha1(en.encode()).hexdigest()[:10], lang, **params)


def shown_commands(text: str, lang: str) -> str:
    """Each command named in a short label («/add · /remove») as a room in this language shows it."""
    return " ".join(shown_command(w, lang) if w.startswith("/") else w for w in text.split(" "))


def topic_key(en: str) -> str:
    """A ready team's topic in the catalog, by its English words."""
    return "topic." + hashlib.sha1(en.encode("utf-8")).hexdigest()[:10]


def tr_key(en: str) -> str:
    return "t." + hashlib.sha1(en.encode("utf-8")).hexdigest()[:10]


# The chat's own language: its room's, set when the chat reads the room's file (and on /language). The chat is one
# room's; the deliverer and the command line say the room's words with the room's language given.
SPOKEN = {"lang": "en"}


def speak(lang: str) -> None:
    SPOKEN["lang"] = lang_of(lang)


def tr(en: str, lang: str = "", **params: Any) -> str:
    """A line the room says, by its English words as a template — «Restart {name}?» — in the room's language (the
    chat's when none is given), its parameters put in whole. Each such template has its Russian one in the catalog
    (tests read every call)."""
    lang = lang_of(lang or SPOKEN["lang"])
    entry = CATALOG.get(tr_key(en))
    text = en if lang == "en" or entry is None else str(entry[lang])
    said = text.format(**params) if params else text
    # A template of marks and places only («{who} → {to} › ») lays a line out: no words of its own to check.
    return pseudo(said) if any(c.isalpha() for c in re.sub(r"\{\w*\}", "", en)) else said


def status_word(en: str, lang: str = "") -> str:
    """A status word as the chat shows it: its English stays the key of its colour and mark."""
    return words_in("status." + en, lang or SPOKEN["lang"]) if "status." + en in CATALOG else en


def state_words(state: str, en: str, lang: str) -> str:
    """What an agent's state says after its name («is working»), in the room's language."""
    return words_in("state." + state, lang) if "state." + state in CATALOG else en


def retell(m: dict[str, Any]) -> str:
    """A message's text as the chat shows it: the room's own line (a `said` template from the room) in the chat's
    language; anything else — and anything that does not fit — as it was written."""
    said = m.get("said")
    if m.get("from") != "room" or not isinstance(said, dict) or SPOKEN["lang"] == "en":
        return str(m.get("text", ""))
    t, p = said.get("t"), said.get("p")
    entry = CATALOG.get(tr_key(t)) if isinstance(t, str) else None
    if entry is None or not isinstance(p, dict):
        return str(m.get("text", ""))
    try:
        return str(entry[SPOKEN["lang"]]).format(**{str(k): str(v) for k, v in p.items()})
    except (KeyError, IndexError, ValueError):
        return str(m.get("text", ""))
